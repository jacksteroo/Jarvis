import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { logInfo } from './logger'

logInfo('bootstrap', 'app_bootstrap_start')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)

logInfo('bootstrap', 'app_bootstrap_render_called')
