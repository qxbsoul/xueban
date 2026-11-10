import Sidebar from './components/Sidebar';
import Topbar from './components/Topbar';
import WorkbenchView from './components/WorkbenchView';
import ApprovalsView from './components/ApprovalsView';
import LogsView from './components/LogsView';
import OrgView from './components/OrgView';
import TasksView from './components/TasksView';
import DeliverablesView from './components/DeliverablesView';
import DashboardView from './components/DashboardView';
import KnowledgeView from './components/KnowledgeView';
import SettingsView from './components/SettingsView';
import AuthView from './components/AuthView';
import ModalHost from './components/ModalHost';
import Toasts from './components/Toasts';
import LandingPage from './landing/LandingPage';
import { WorkbenchContext, useWorkbenchStore } from './store';

/** 学情工作台路由和参考站一致：/app；其余路径进入介绍页。 */
const APP_PATH = /^\/app(?:\/|$)/;

export default function App() {
  const inApp = APP_PATH.test(window.location.pathname) || window.location.hash === '#/app';
  if (!inApp) return <LandingPage />;
  return <WorkbenchApp />;
}

function WorkbenchApp() {
  const store = useWorkbenchStore();

  return (
    <WorkbenchContext.Provider value={store}>
      <div className="app">
        <Sidebar />
        <div className="main">
          {store.authStatus === 'loading' ? (
            <div className="auth-loading">正在确认账号与教研空间…</div>
          ) : store.authStatus === 'anonymous' ? (
            <AuthView />
          ) : (
            <>
              <Topbar />
              <div className="views">
                <section className={store.view === 'workbench' ? 'view active' : 'view'}><WorkbenchView /></section>
                <section className={store.view === 'approvals' ? 'view active' : 'view'}><ApprovalsView /></section>
                <section className={store.view === 'logs' ? 'view active' : 'view'}><LogsView /></section>
                <section className={store.view === 'org' ? 'view active' : 'view'}><OrgView /></section>
                <section className={store.view === 'tasks' ? 'view active' : 'view'}><TasksView /></section>
                <section className={store.view === 'deliverables' ? 'view active' : 'view'}><DeliverablesView /></section>
                <section className={store.view === 'dashboard' ? 'view active' : 'view'}><DashboardView /></section>
                <section className={store.view === 'knowledge' ? 'view active' : 'view'}><KnowledgeView /></section>
                <section className={store.view === 'settings' ? 'view active' : 'view'}><SettingsView /></section>
              </div>
            </>
          )}
        </div>
      </div>
      <ModalHost />
      <Toasts />
    </WorkbenchContext.Provider>
  );
}
