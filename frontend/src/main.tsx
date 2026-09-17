import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Shop from './Shop.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {new URLSearchParams(location.search).get('demo') === '1' ||
    location.pathname === '/legacy-orders' ? (
      <App />
    ) : (
      <Shop />
    )}
  </StrictMode>,
)
