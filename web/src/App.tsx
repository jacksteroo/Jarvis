import { useEffect, useState } from 'react'
import Layout from './components/Layout'
import Chat from './components/Chat'
import Status from './components/Status'
import LifeContext from './components/LifeContext'
import Relationships from './components/Relationships'
import Traces from './components/Traces'
import { logInfo } from './logger'

type Tab = 'chat' | 'status' | 'context' | 'relationships' | 'traces'

export default function App() {
  const [tab, setTab] = useState<Tab>('chat')

  useEffect(() => {
    logInfo('app', 'mounted', { initialTab: tab })

    return () => {
      logInfo('app', 'unmounted')
    }
  }, [])

  useEffect(() => {
    logInfo('app', 'tab_changed', { tab })
  }, [tab])

  return (
    <Layout tab={tab} onTabChange={setTab}>
      {tab === 'chat' && <Chat />}
      {tab === 'status' && <Status />}
      {tab === 'context' && <LifeContext />}
      {tab === 'relationships' && <Relationships />}
      {tab === 'traces' && <Traces />}
    </Layout>
  )
}
