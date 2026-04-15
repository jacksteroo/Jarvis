import asyncio
import structlog
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from agent.briefs import CommitmentExtractor, BriefFormatter
from agent.models import AuditLog

logger = structlog.get_logger()


class PepperScheduler:
    def __init__(self, pepper_core, config, telegram_bot=None):
        self.pepper = pepper_core
        self.config = config
        self.bot = telegram_bot
        self.formatter = BriefFormatter()
        self.extractor = CommitmentExtractor(llm_client=getattr(pepper_core, 'llm', None))
        self._scheduler = AsyncIOScheduler()
        self._last_brief: datetime = None
        self._last_review: datetime = None

    def start(self):
        """Register all jobs and start the scheduler."""
        # Morning brief
        self._scheduler.add_job(
            self.generate_morning_brief,
            CronTrigger(hour=self.config.MORNING_BRIEF_HOUR, minute=self.config.MORNING_BRIEF_MINUTE),
            id="morning_brief",
            replace_existing=True,
        )

        # Commitment check — daily at noon
        self._scheduler.add_job(
            self.check_commitments,
            CronTrigger(hour=12, minute=0),
            id="commitment_check",
            replace_existing=True,
        )

        # Weekly review — configurable day/hour
        self._scheduler.add_job(
            self.generate_weekly_review,
            CronTrigger(day_of_week=self.config.WEEKLY_REVIEW_DAY, hour=self.config.WEEKLY_REVIEW_HOUR, minute=0),
            id="weekly_review",
            replace_existing=True,
        )

        # Memory compression — Sunday 02:00 UTC
        self._scheduler.add_job(
            self.run_memory_compression,
            CronTrigger(day_of_week=6, hour=2, minute=0),
            id="memory_compression",
            replace_existing=True,
        )

        self._scheduler.start()
        logger.info("scheduler_started", jobs=[j.id for j in self._scheduler.get_jobs()])

    def stop(self):
        self._scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")

    # ─── Jobs ──────────────────────────────────────────────────────────────

    async def generate_morning_brief(self) -> str:
        logger.info("generating_morning_brief")
        today = datetime.now().strftime("%A, %B %-d, %Y")

        # Try calendar subsystem (graceful degradation)
        calendar_summary = ""
        try:
            result = await self.pepper.tool_router.call_tool(
                "calendar", "get_upcoming_events", {"days": 1}
            )
            if "error" not in result:
                events = result.get("result", result)
                if isinstance(events, list):
                    calendar_summary = "\n".join(
                        f"  {e.get('time', '')} — {e.get('title', e.get('summary', ''))}"
                        for e in events[:5]
                    )
                elif isinstance(events, str):
                    calendar_summary = events
        except Exception as e:
            logger.warning("calendar_unavailable", error=str(e))

        # Pull open loops from memory
        open_loops = await self.pepper.memory.search_recall(
            "open loop OR unresolved OR pending OR waiting", limit=5
        )

        # Pull pending commitments
        commitments = await self.pepper.memory.search_recall(
            "COMMITMENT: OR I will OR follow up OR I'll send OR I'll intro", limit=5
        )
        # Filter out resolved ones
        commitments = [c for c in commitments if not c.get("content", "").startswith("[RESOLVED]")]

        # Comms health signal (graceful degradation)
        comms_health_section = ""
        try:
            from agent.comms_health_tools import get_comms_health_brief_section
            comms_health_section = await get_comms_health_brief_section(quiet_days=14)
        except Exception as e:
            logger.warning("comms_health_brief_skip", error=str(e))

        # Generate brief via LLM if we have enough context
        formatted = self.formatter.format_morning_brief(today, calendar_summary, open_loops, commitments)
        if comms_health_section:
            formatted += f"\n\n{comms_health_section}"

        # Synthesize with Pepper LLM for a more natural brief
        try:
            result = await self.pepper.llm.chat(
                messages=[{
                    "role": "system",
                    "content": self.pepper._system_prompt,
                }, {
                    "role": "user",
                    "content": (
                        f"Generate my morning brief for {today}. "
                        f"Calendar: {calendar_summary or 'no events found'}. "
                        f"Open loops: {[o.get('content', '')[:80] for o in open_loops]}. "
                        f"Pending commitments: {[c.get('content', '')[:80] for c in commitments]}. "
                        "Be brief, direct, actionable. Lead with the most important thing. Max 5 bullet points."
                    )
                }],
                model=f"local/{self.config.DEFAULT_LOCAL_MODEL}"
            )
            brief_text = result.get("content") or formatted
        except Exception as e:
            logger.warning("brief_llm_failed", error=str(e))
            brief_text = formatted

        # Save to memory
        await self.pepper.memory.save_to_recall(
            f"Morning brief sent: {today}\n{brief_text[:300]}", importance=0.7
        )

        # Push via Telegram
        await self._send(brief_text)

        # Audit log
        await self._audit("morning_brief_sent", f"Brief for {today}")
        self._last_brief = datetime.utcnow()

        logger.info("morning_brief_sent", date=today)
        return brief_text

    async def check_commitments(self) -> list[dict]:
        logger.info("checking_commitments")
        results = await self.pepper.memory.search_recall(
            "COMMITMENT: OR I will OR follow up OR I'll send OR I'll intro OR I'll reach out",
            limit=15
        )

        # Filter: unresolved and older than 48h
        cutoff = datetime.utcnow() - timedelta(hours=48)
        pending = []
        for r in results:
            content = r.get("content", "")
            if content.startswith("[RESOLVED]"):
                continue
            try:
                created = datetime.fromisoformat(r.get("created_at", ""))
                if created < cutoff:
                    pending.append(r)
            except Exception:
                pass

        if pending:
            lines = ["⏰ **Commitment reminder** — these are still open:\n"]
            for p in pending[:5]:
                lines.append(f"• {p.get('content', '')[:120]}")
            message = "\n".join(lines)
            await self._send(message)
            await self._audit("commitment_check", f"Reminded about {len(pending)} commitments")
        else:
            await self._audit("commitment_check", "No pending commitments found")

        return pending

    async def generate_weekly_review(self) -> str:
        logger.info("generating_weekly_review")
        week_label = datetime.now().strftime("Week of %B %-d, %Y")

        memories = await self.pepper.memory.get_recent_recall(days=7)
        commitments = await self.check_commitments()

        formatted = self.formatter.format_weekly_review(week_label, memories, commitments)

        try:
            result = await self.pepper.llm.chat(
                messages=[{
                    "role": "system",
                    "content": self.pepper._system_prompt,
                }, {
                    "role": "user",
                    "content": (
                        f"Generate my weekly review for {week_label}. "
                        f"This week's memories: {[getattr(m, 'content', str(m))[:80] for m in memories[:10]]}. "
                        "Summarize what happened, what's still open, and what needs attention next week."
                    )
                }],
                model=f"local/{self.config.DEFAULT_LOCAL_MODEL}"
            )
            review_text = result.get("content") or formatted
        except Exception as e:
            logger.warning("review_llm_failed", error=str(e))
            review_text = formatted

        await self.pepper.memory.save_to_recall(
            f"Weekly review: {week_label}\n{review_text[:300]}", importance=0.8
        )
        await self._send(review_text)
        await self._audit("weekly_review_sent", week_label)
        self._last_review = datetime.utcnow()

        return review_text

    async def run_memory_compression(self) -> dict:
        logger.info("running_memory_compression")
        result = await self.pepper.memory.compress_to_archival()
        await self._audit("memory_compression", str(result))
        return result

    def get_status(self) -> dict:
        return {
            "jobs": [j.id for j in self._scheduler.get_jobs()],
            "last_brief": self._last_brief.isoformat() if self._last_brief else None,
            "last_review": self._last_review.isoformat() if self._last_review else None,
            "running": self._scheduler.running,
        }

    # ─── Helpers ───────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        if self.bot:
            try:
                await self.bot.send_message(text)
            except Exception as e:
                logger.warning("telegram_send_failed", error=str(e))
        else:
            logger.info("brief_output", text=text[:200])

    async def _audit(self, event_type: str, details: str = "") -> None:
        if not self.pepper.db_factory:
            return
        try:
            async with self.pepper.db_factory() as session:
                session.add(AuditLog(event_type=event_type, details=details))
                await session.commit()
        except Exception as e:
            logger.warning("audit_log_failed", error=str(e))
