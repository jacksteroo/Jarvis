import asyncio
import random
import re
import structlog
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction
from agent.config import Settings
from agent.models import AuditLog

logger = structlog.get_logger()

_THINKING_STARS = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢", "·"]  # forward then reverse
_CURSORS = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
_CURSOR_INTERVAL = 0.5

_FALLBACK_ACKS = [
    "Got it, working on that now.",
    "On it, give me a moment.",
    "Right away.",
    "Sure, let me get that sorted.",
]

_ACK_SYSTEM_PROMPT = (
    "You are Pepper, a sharp AI chief of staff. You are a single AI — never say 'we', 'our', "
    "'my team', or imply you are a group. Always use first-person singular (I, me, my). "
    "Write a brief conversational acknowledgment (2 sentences max) of what the user is asking. "
    "First sentence: rephrase what they want in plain language so they know you understood. "
    "Second sentence: say you're working on it — natural, not robotic. "
    "No emojis. No bullet points. No 'certainly' or 'of course'. Keep it under 30 words total."
)

class JARViSTelegramBot:
    def __init__(self, token: str, pepper_core, config: Settings):
        self.token = token
        self.pepper = pepper_core
        self.config = config
        self._allowed_ids = config.get_allowed_telegram_user_ids()
        self._app: Application = None
        self._bot: Bot = None
        # Per-user "edit mode" state: when the user taps Edit, the next plain
        # text message they send replaces the draft body for that action.
        self._edit_pending: dict[int, str] = {}  # telegram_user_id → action_id

    async def setup(self) -> None:
        """Build the Application and register all handlers."""
        self._app = Application.builder().token(self.token).build()
        self._bot = self._app.bot

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("brief", self._cmd_brief))
        self._app.add_handler(CommandHandler("review", self._cmd_review))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pending", self._cmd_pending))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(
            CallbackQueryHandler(self._handle_action_callback, pattern=r"^pa:")
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        # Capture 👍 / 👎 / etc. reactions on Pepper's outbound messages and
        # turn them into explicit success_signal rows. The success-signal
        # heuristic infers from text follow-ups; reactions give a stronger,
        # one-tap channel for the migration's signal-coverage gate.
        self._app.add_handler(MessageReactionHandler(self._handle_reaction))
        self._app.add_error_handler(self._error_handler)

    async def start(self) -> None:
        """Start polling. Runs until stop() is called."""
        await self.setup()
        await self._app.initialize()
        await self._app.start()
        # Reactions are not in the default allowed_updates set — we have to
        # opt in explicitly or Telegram won't deliver MessageReactionUpdated
        # at all. Subscribe to all update types python-telegram-bot knows
        # about so future feedback channels (polls, edits) are covered too.
        await self._app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("telegram_bot_started")

    async def stop(self) -> None:
        if self._app:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        logger.info("telegram_bot_stopped")

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> None:
        """Push a message to all allowed users (or just first one for single-user setup)."""
        if not self._bot:
            logger.warning("telegram_send_skipped", reason="bot not initialized")
            return
        # For single-user setup, TELEGRAM_ALLOWED_USER_IDS should have one entry
        if self._allowed_ids:
            for user_id in self._allowed_ids:
                try:
                    await self._send_long(user_id, text, parse_mode)
                except Exception as e:
                    logger.error("telegram_push_failed", user_id=user_id, error=str(e))
        else:
            logger.warning("telegram_no_recipients", reason="TELEGRAM_ALLOWED_USER_IDS not set")

    # ─── Auth ──────────────────────────────────────────────────────────────

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed_ids:
            return True  # no restriction set — single-user assumption
        return user_id in self._allowed_ids

    async def _check_auth(self, update: Update) -> bool:
        user_id = update.effective_user.id
        if not self._is_allowed(user_id):
            await update.message.reply_text("Unauthorized.")
            await self._audit(f"unauthorized_access user_id={user_id}")
            logger.warning("unauthorized_telegram_access", user_id=user_id)
            return False
        return True

    # ─── Command Handlers ──────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await update.message.reply_text(
            "🤖 *Pepper is Online.*\n\n"
            "I'm your personal AI chief of staff. I know your life context and I'm here to help you navigate it.\n\n"
            "Ask me anything — about your life, your relationships, what you should focus on, or what you're avoiding.\n\n"
            "Use /help to see available commands.",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await update.message.reply_text(
            "*Commands for Pepper*\n\n"
            "/brief — Generate morning brief now\n"
            "/review — Generate weekly review now\n"
            "/status — System status (subsystems, memory, scheduler)\n"
            "/pending — List drafts awaiting your approval\n"
            "/help — Show this message\n\n"
            "Or just send any message to talk to Pepper.",
            parse_mode="Markdown",
        )

    async def _cmd_brief(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        try:
            # Access scheduler through pepper
            scheduler = getattr(self.pepper, '_scheduler', None)
            if scheduler:
                brief = await scheduler.generate_morning_brief()
            else:
                brief = "Scheduler not initialized yet."
            await self._send_long(update.effective_chat.id, brief)
        except Exception as e:
            logger.error("cmd_brief_failed", error=str(e))
            await update.message.reply_text("Failed to generate brief. Check logs.")

    async def _cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        try:
            scheduler = getattr(self.pepper, '_scheduler', None)
            if scheduler:
                review = await scheduler.generate_weekly_review()
            else:
                review = "Scheduler not initialized yet."
            await self._send_long(update.effective_chat.id, review)
        except Exception as e:
            logger.error("cmd_review_failed", error=str(e))
            await update.message.reply_text("Failed to generate review. Check logs.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        try:
            s = await self.pepper.get_status()
            lines = ["*Pepper Status*\n"]
            lines.append(f"{'✅' if s.get('initialized') else '❌'} Core initialized")

            subsystems = s.get("subsystems", {})
            if subsystems:
                lines.append("\n*Subsystems:*")
                for name, health in subsystems.items():
                    icon = "✅" if health == "ok" else ("⚠️" if health == "degraded" else "❌")
                    lines.append(f"{icon} {name}: {health}")

            sched = s.get("scheduler", {})
            if sched:
                lines.append(f"\n*Scheduler:* {'running' if sched.get('running') else 'stopped'}")
                if sched.get("last_brief"):
                    lines.append(f"Last brief: {sched['last_brief'][:16]}")

            lines.append(f"\n*Working memory:* {s.get('working_memory_size', 0)} messages")
            lines.append(f"*Local model:* {s.get('default_local_model', 'unknown')}")

            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            logger.error("cmd_status_failed", error=str(e))
            await update.message.reply_text("Failed to fetch status.")

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler — log the exception so it doesn't surface as an unhandled crash."""
        logger.error(
            "telegram_unhandled_error",
            error=str(context.error),
            update=str(update)[:200] if update else None,
        )

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Guard against updates where message or text is None (e.g. edited messages,
        # channel posts, or inline-query results that slip through the TEXT filter).
        if update.message is None or update.message.text is None:
            logger.debug(
                "telegram_non_text_update_skipped",
                update_id=getattr(update, "update_id", None),
            )
            return
        if not await self._check_auth(update):
            return
        user_message = update.message.text
        session_id = str(update.effective_user.id)

        # Edit-mode interception: if the user previously tapped ✏️ Edit on a
        # draft, treat their next plain message as the replacement body and
        # short-circuit the normal chat path. /cancel exits edit mode.
        editing_action_id = self._edit_pending.get(update.effective_user.id)
        if editing_action_id:
            if user_message.strip().lower() in {"/cancel", "cancel"}:
                self._consume_edit_target(update.effective_user.id)
                await update.message.reply_text("Edit cancelled — draft kept as-is.")
                return
            self._consume_edit_target(update.effective_user.id)
            edited = self.pepper.pending_actions.edit(editing_action_id, user_message)
            if not edited:
                await update.message.reply_text(
                    f"Couldn't edit `{editing_action_id}` — it may have already been sent or rejected.",
                    parse_mode="Markdown",
                )
                return
            try:
                await update.message.reply_text(
                    self._format_pending(edited.to_dict()),
                    parse_mode="Markdown",
                    reply_markup=self._approval_keyboard(edited.id),
                )
            except Exception:
                await update.message.reply_text(
                    self._format_pending(edited.to_dict()),
                    reply_markup=self._approval_keyboard(edited.id),
                )
            return

        logger.info("telegram_in", user_id=session_id, text=user_message[:300])

        # Phase 3 cutover: route via the SemanticRouter primary so the
        # heavy/light decision is consistent with the routing chat() will
        # use. The capability-filter post-step is applied inside the helper.
        try:
            prerouted = await self.pepper.route_with_capability_filter(user_message)
        except Exception as exc:
            logger.warning("telegram_route_failed", error=str(exc))
            prerouted = None
        heavy, reason = self.pepper.decide_query_depth(
            user_message, all_routings=prerouted
        )
        logger.debug("telegram_query_depth", heavy=heavy, reason=reason, text=user_message[:80])
        chat_task = asyncio.create_task(
            self.pepper.chat(user_message, session_id, heavy=heavy, channel="Telegram")
        )

        if heavy:
            # Heavy query — first send a context-aware ack that regurgitates
            # the request so the user knows we understood, then show a spinner
            # while data is being fetched and reasoned over.
            try:
                ack_result = await asyncio.wait_for(
                    self.pepper.llm.chat(
                        [
                            {"role": "system", "content": _ACK_SYSTEM_PROMPT},
                            {"role": "user", "content": user_message},
                        ]
                    ),
                    timeout=4,
                )
                ack_text = (ack_result.get("content") or "").strip()
                if not ack_text:
                    raise ValueError("empty ack")
            except Exception:
                ack_text = random.choice(_FALLBACK_ACKS)

            await self._stream_response(update.effective_chat.id, ack_text)

            status_msg = [None]
            status_msg[0] = await update.message.reply_text(
                rf"`{_THINKING_STARS[0]}` _Thinking\.\.\._",
                parse_mode="MarkdownV2",
            )

            async def _animate():
                frame = 1
                while True:
                    await asyncio.sleep(0.25)
                    star = _THINKING_STARS[frame % len(_THINKING_STARS)]
                    try:
                        await status_msg[0].edit_text(
                            rf"`{star}` _Thinking\.\.\._",
                            parse_mode="MarkdownV2",
                        )
                    except Exception:
                        pass
                    frame += 1
                    if frame % 8 == 0:
                        try:
                            await context.bot.send_chat_action(
                                chat_id=update.effective_chat.id, action=ChatAction.TYPING
                            )
                        except Exception:
                            pass

            animator = asyncio.create_task(_animate())
            try:
                response = await chat_task
                logger.info("telegram_out", user_id=session_id, text=response[:300])
                try:
                    await status_msg[0].delete()
                except Exception:
                    pass
                if not response:
                    response = "I wasn't able to generate a response. Please try again."
                last_msg_id = await self._render_response(update.effective_chat.id, response)
                await self._record_outbound(session_id, update.effective_chat.id, last_msg_id)
            except Exception as e:
                logger.error("message_handler_failed", error=str(e), exc_info=True)
                try:
                    await status_msg[0].delete()
                except Exception:
                    pass
                await update.message.reply_text("Something went wrong on my end. Please try again.")
            finally:
                chat_task.cancel()
                animator.cancel()
        else:
            # Simple query — stream the answer directly, no spinner
            try:
                response = await chat_task
                logger.info("telegram_out", user_id=session_id, text=response[:300])
                if not response:
                    response = "I wasn't able to generate a response. Please try again."
                last_msg_id = await self._render_response(update.effective_chat.id, response)
                await self._record_outbound(session_id, update.effective_chat.id, last_msg_id)
            except Exception as e:
                logger.error("message_handler_failed", error=str(e), exc_info=True)
                await update.message.reply_text("Something went wrong on my end. Please try again.")
            finally:
                chat_task.cancel()

    async def _record_outbound(
        self, session_id: str, chat_id: int, message_id: int | None
    ) -> None:
        """Best-effort: stamp the outbound message id onto the routing_events row."""
        if message_id is None:
            return
        try:
            await self.pepper.record_outbound_message(
                session_id=session_id, chat_id=chat_id, message_id=message_id
            )
        except Exception as exc:
            logger.warning("telegram_outbound_record_failed", error=str(exc))

    # ─── Helpers ───────────────────────────────────────────────────────────

    async def _send_image(self, chat_id: int, url: str) -> bool:
        """Send a single image by URL. Returns True on success."""
        try:
            await self._bot.send_photo(chat_id=chat_id, photo=url)
            logger.info("telegram_photo_sent", url=url)
            return True
        except Exception as e:
            logger.warning("telegram_photo_failed", url=url, error=str(e))
            return False

    async def _render_response(self, chat_id: int, text: str) -> int | None:
        """Render a response, extracting any [IMAGE:url] markers and sending them as photos.

        Returns the message_id of the final text message sent (the one a
        user is most likely to react to). ``None`` when nothing was sent.
        """
        image_pattern = re.compile(r"\[IMAGE:([^\]]+)\]")
        images = image_pattern.findall(text)
        clean_text = image_pattern.sub("", text).strip()

        for url in images:
            await self._send_image(chat_id, url.strip())

        if clean_text:
            return await self._stream_response(chat_id, clean_text)
        return None

    async def _send_long(self, chat_id, text: str, parse_mode: str = "Markdown") -> None:
        """Send text, splitting into chunks if > 4096 chars (Telegram limit)."""
        if not text:
            return
        chunk_size = 4096
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size]
            try:
                await self._bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
            except Exception:
                # Fallback: send without markdown if parse fails
                await self._bot.send_message(chat_id=chat_id, text=chunk)

    async def _stream_response(self, chat_id: int, text: str) -> int | None:
        """Sentence-by-sentence reveal with spinning braille cursor pause between each.

        Returns the message_id of the final paragraph sent so the caller
        can record it for reaction-based feedback capture.
        """
        paragraphs = [p.strip() for p in re.split(r'\n{2,}', text.strip()) if p.strip()]
        if not paragraphs:
            return None

        cursor_frame = 0
        last_message_id: int | None = None

        for para in paragraphs:
            if len(para) > 4000:
                await self._send_long(chat_id, para)
                continue

            # Find sentence cut points within the original para (preserves newlines/formatting)
            cut_points = [m.end() for m in re.finditer(r'[.!?](?=\s|$)', para)]
            if not cut_points or cut_points[-1] < len(para):
                cut_points.append(len(para))

            msg = await self._bot.send_message(chat_id=chat_id, text=_CURSORS[0])
            last_message_id = msg.message_id
            prev_end = 0

            for end_pos in cut_points:
                accumulated = para[:end_pos]
                sentence_len = end_pos - prev_end
                prev_end = end_pos

                pause = max(1.0, min(3.0, sentence_len / 25))
                steps = round(pause / _CURSOR_INTERVAL)

                try:
                    await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        text=accumulated + " " + _CURSORS[cursor_frame % len(_CURSORS)],
                    )
                except Exception:
                    pass

                for _ in range(steps):
                    await asyncio.sleep(_CURSOR_INTERVAL)
                    cursor_frame += 1
                    try:
                        await self._bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg.message_id,
                            text=accumulated + " " + _CURSORS[cursor_frame % len(_CURSORS)],
                        )
                    except Exception:
                        pass

            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    text=para,
                    parse_mode="Markdown",
                )
            except Exception:
                try:
                    await self._bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg.message_id,
                        text=para,
                    )
                except Exception:
                    pass

        return last_message_id

    # ─── Pending-action approval flow ──────────────────────────────────────

    @staticmethod
    def _channel_for_tool(tool_name: str) -> str:
        return {
            "send_email": "✉️ Email",
            "send_imessage": "💬 iMessage",
            "send_whatsapp": "🟢 WhatsApp",
            "create_calendar_event": "📅 Calendar",
        }.get(tool_name, tool_name)

    def _format_pending(self, item: dict) -> str:
        """Build the message body that the approve/reject buttons attach to."""
        tool = item.get("tool_name", "")
        args = item.get("args", {}) or {}
        chan = self._channel_for_tool(tool)
        lines = [f"*{chan} draft* — `id={item.get('id', '')}`"]
        if tool == "send_email":
            lines.append(f"*Account:* {args.get('account', 'default')}")
            lines.append(f"*To:* {args.get('to', '')}")
            if args.get("cc"):
                lines.append(f"*Cc:* {args['cc']}")
            lines.append(f"*Subject:* {args.get('subject', '')}")
            lines.append("")
            lines.append((args.get("body") or "")[:1500])
        elif tool == "send_imessage":
            target = args.get("chat_guid") or args.get("to") or ""
            lines.append(f"*To:* {target}")
            lines.append("")
            lines.append((args.get("body") or "")[:1500])
        elif tool == "send_whatsapp":
            lines.append(f"*Chat:* {args.get('chat_id', '')}")
            lines.append("")
            lines.append((args.get("body") or args.get("message") or "")[:1500])
        elif tool == "create_calendar_event":
            lines.append(f"*Calendar:* {args.get('calendar_id', 'primary')} ({args.get('account', 'default')})")
            lines.append(f"*Title:* {args.get('summary', '')}")
            when = args.get("start", "")
            until = args.get("end", "")
            if when or until:
                lines.append(f"*When:* {when} → {until}")
            if args.get("location"):
                lines.append(f"*Where:* {args['location']}")
            if args.get("attendees"):
                lines.append(f"*Attendees:* {args['attendees']}")
            if args.get("description"):
                lines.append("")
                lines.append((args["description"] or "")[:1500])
        else:
            lines.append(item.get("preview", ""))
        if item.get("model_description"):
            lines.append("")
            lines.append(f"_model says:_ {item['model_description'][:300]}")
        return "\n".join(lines)

    @staticmethod
    def _approval_keyboard(action_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Send", callback_data=f"pa:approve:{action_id}"),
                InlineKeyboardButton("✏️ Edit",  callback_data=f"pa:edit:{action_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"pa:reject:{action_id}"),
            ]]
        )

    async def notify_pending_action(self, item) -> None:
        """Push a freshly-queued draft to allowed users with inline buttons.

        Wired via PendingActionsQueue.set_notifier in start.py. Best-effort —
        any send failure is logged but never propagated back to the queue.
        """
        if not self._bot or not self._allowed_ids:
            return
        try:
            payload = item.to_dict() if hasattr(item, "to_dict") else item
        except Exception:
            payload = {}
        body = self._format_pending(payload)
        markup = self._approval_keyboard(payload.get("id", ""))
        for user_id in self._allowed_ids:
            try:
                await self._bot.send_message(
                    chat_id=user_id, text=body, parse_mode="Markdown", reply_markup=markup,
                )
            except Exception:
                # Markdown can choke on user-supplied text; fall back to plain.
                try:
                    await self._bot.send_message(
                        chat_id=user_id, text=body, reply_markup=markup,
                    )
                except Exception as exc:
                    logger.warning("telegram_pending_notify_failed", user_id=user_id, error=str(exc))

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        items = self.pepper.pending_actions.list_pending()
        if not items:
            await update.message.reply_text("No drafts pending approval.")
            return
        for item in items:
            try:
                await update.message.reply_text(
                    self._format_pending(item),
                    parse_mode="Markdown",
                    reply_markup=self._approval_keyboard(item.get("id", "")),
                )
            except Exception:
                await update.message.reply_text(
                    self._format_pending(item),
                    reply_markup=self._approval_keyboard(item.get("id", "")),
                )

    async def _handle_action_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Inline-button handler for pa:approve / pa:edit / pa:reject."""
        query = update.callback_query
        if query is None or not query.data:
            return
        if not self._is_allowed(query.from_user.id):
            await query.answer("Unauthorized.", show_alert=True)
            return
        try:
            _, action, action_id = query.data.split(":", 2)
        except ValueError:
            await query.answer("Bad callback.", show_alert=True)
            return

        item = self.pepper.pending_actions.get(action_id)
        if not item:
            await query.answer("Draft not found (already handled?).", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        if action == "approve":
            await query.answer("Sending…")
            executed = await self.pepper.pending_actions.approve(action_id)
            await self._finalize_action_message(query, executed)
        elif action == "reject":
            self.pepper.pending_actions.reject(action_id)
            await query.answer("Rejected.")
            try:
                await query.edit_message_text(
                    self._format_pending(item.to_dict()) + "\n\n_❌ rejected_",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        elif action == "edit":
            self._edit_pending[query.from_user.id] = action_id
            await query.answer("Send the new body as your next message.")
            try:
                await self._bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        f"✏️ Edit mode for `{action_id}` — send the replacement body as your "
                        "next message. Send /cancel to abort."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        else:
            await query.answer("Unknown action.", show_alert=True)

    async def _finalize_action_message(self, query, executed) -> None:
        """Replace the inline-button message with the post-execution status."""
        if executed is None:
            try:
                await query.edit_message_text("Draft not found.")
            except Exception:
                pass
            return
        snapshot = executed.to_dict()
        status = snapshot.get("status", "?")
        result = snapshot.get("result") or {}
        if status == "executed":
            tail = "\n\n_✅ sent_"
            if isinstance(result, dict) and result.get("id"):
                tail += f" id=`{result['id']}`"
        else:
            err = (result or {}).get("error", "unknown error") if isinstance(result, dict) else "failed"
            tail = f"\n\n_⚠️ {status}: {err}_"
        body = self._format_pending(snapshot) + tail
        try:
            await query.edit_message_text(body, parse_mode="Markdown")
        except Exception:
            try:
                await query.edit_message_text(body)
            except Exception:
                pass

    def _consume_edit_target(self, user_id: int) -> str | None:
        """Pop the action_id the user is currently editing, if any."""
        return self._edit_pending.pop(user_id, None)

    async def _handle_reaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Map an inbound 👍/👎 (etc.) reaction to a routing_events row."""
        reaction = update.message_reaction
        if reaction is None:
            return
        # Auth: only allowed users' reactions count.
        user = reaction.user
        if user is None or not self._is_allowed(user.id):
            return
        new_emojis = [
            r.emoji for r in (reaction.new_reaction or []) if getattr(r, "emoji", None)
        ]
        try:
            applied = await self.pepper.apply_reaction_signal(
                chat_id=reaction.chat.id,
                message_id=reaction.message_id,
                emojis=new_emojis,
            )
            logger.info(
                "telegram_reaction_received",
                chat_id=reaction.chat.id,
                message_id=reaction.message_id,
                emojis=new_emojis,
                signal_applied=applied,
            )
        except Exception as exc:
            logger.warning("telegram_reaction_handler_failed", error=str(exc))

    async def _audit(self, details: str) -> None:
        if not self.pepper.db_factory:
            return
        try:
            async with self.pepper.db_factory() as session:
                session.add(AuditLog(event_type="telegram_event", details=details))
                await session.commit()
        except Exception:
            pass
