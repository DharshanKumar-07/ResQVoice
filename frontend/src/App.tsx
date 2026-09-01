import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import { Activity, Mic, Radio } from 'lucide-react';
import Dashboard from './components/Dashboard';
import AgoraRoom from './components/AgoraRoom';
import './App.css';

export default function App() {
  return (
    <BrowserRouter>
      <div className="app-layout">
        <header className="app-header">
          <div className="header-brand">
            <div className="logo-badge">
              <Radio size={20} />
            </div>
            <div>
              <div className="brand-title">ResQVoice</div>
              <div className="brand-subtitle">Incident operations</div>
            </div>
          </div>

          <nav>
            <ul className="nav-links">
              <li>
                <NavLink 
                  to="/" 
                  className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
                >
                  <Activity size={16} />
                  <span>Command Center</span>
                </NavLink>
              </li>
              <li>
                <NavLink 
                  to="/room" 
                  className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
                >
                  <Mic size={16} />
                  <span>Voice Room</span>
                </NavLink>
              </li>
            </ul>
          </nav>

          <div className="nav-status">
            <div className="pulse-dot" />
            <span>Voice Bridge Live</span>
          </div>
        </header>

        <main className="main-content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/room" element={<AgoraRoom />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
