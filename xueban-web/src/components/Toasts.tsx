import { useWorkbench } from '../store';

export default function Toasts() {
  const { toasts } = useWorkbench();
  return (
    <div className="toast-wrap">
      {toasts.map(t => (
        <div className="toast" key={t.id}>
          {t.msg}
        </div>
      ))}
    </div>
  );
}
