import { lazy, Suspense } from 'react';
import { ToastHost } from './components/Toast';

const Dashboard = lazy(() => import('./components/Dashboard'));

export default function App() {
  return (
    <div id="app-container" className="w-full h-screen bg-background flex flex-col font-sans text-on-surface antialiased overflow-hidden">
      <ToastHost />
      <div className="flex-1 flex overflow-hidden">
        <Suspense fallback={<div className="flex h-full w-full items-center justify-center bg-background text-sm text-on-surface-variant">Loading workspace…</div>}>
          <Dashboard />
        </Suspense>
      </div>
    </div>
  );
}
