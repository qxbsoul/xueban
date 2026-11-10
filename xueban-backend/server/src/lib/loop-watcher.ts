/**
 * loop-watcher — 迭代环实时上流（2026-08-19 导航架构统一批）。
 *
 * 背景：loop（迭代环）在数据模型上是 workflow DAG 的 control.loop 节点，
 * 迭代在 Python RuntimeService 内部执行；Node 侧 CLI 流式管线只能看到
 * 节点级开始/结束，拿不到每轮迭代的细粒度事件。
 *
 * 做法：run 活跃期间（run-sessions live 路径 claimStart 成功后）短周期探测
 * Python 的 GET /api/runs/:id/loops，按 loop_id 对 {status, iteration_count}
 * 做差分签名，变化才经 runBus 发命名 wire 事件 `loop`（EVENT_NAMING §1 已
 * 注册；发射点在 run-sessions.ts）。前端 useRunSession 消费后喂右栏「过程」
 * tab，替代前端各自拉取。
 *
 * 容错：Python 不可达 / 404（普通对话 run 没有 Python run 记录）/ 超时——
 * 一律静默放弃。watcher 是增强通道，不是 run 执行的依赖。
 */
import { PYTHON_BACKEND_URL } from '../python-backend';

const POLL_INTERVAL_MS = 2500;
const FETCH_TIMEOUT_MS = 2000;

/** loops 投影响应里的单条（step 分组，loop 对象在 .loop 字段）。 */
interface LoopsResponse {
  loops?: Array<{ loop?: Record<string, unknown> }>;
}

/**
 * 启动 watcher。返回停止函数（run 终态时调用）。
 *
 * @param sessionId run-session id（与 Python run id 同一空间——
 *                  GET /api/run-sessions/:id/stream 的 live 路径驱动同一 id）
 * @param emit      变化帧回调：payload = { loops: LoopRecord[] }（原样透传
 *                  Python 投影的 loop 对象数组）
 */
export function startLoopWatcher(
  sessionId: string,
  emit: (payload: { loops: Record<string, unknown>[] }) => void,
): () => void {
  let prevSig: string | null = null;
  let stopped = false;

  const timer = setInterval(() => {
    if (stopped) return;
    void (async () => {
      try {
        const res = await fetch(
          `${PYTHON_BACKEND_URL}/api/runs/${encodeURIComponent(sessionId)}/loops`,
          { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) },
        );
        if (!res.ok) return; // 无 Python run 记录（普通对话）等——静默
        const json = (await res.json()) as LoopsResponse;
        const loops = (json.loops ?? [])
          .map((s) => s.loop)
          .filter((l): l is Record<string, unknown> => !!l);
        if (loops.length === 0) return;
        // 差分签名：loop_id × 状态 × 轮数 × 收敛。仅展示相关字段参与签名，
        // metrics 数值波动不触发重发（帧保持可预测的低频）。
        const sig = JSON.stringify(
          loops.map((l) => [
            l.loop_id,
            l.loop_status ?? l.status,
            l.loop_iterations ?? l.iteration_count,
            l.loop_converged ?? false,
          ]),
        );
        if (sig === prevSig) return;
        prevSig = sig;
        emit({ loops });
      } catch {
        // Python 掉线 / 超时：watcher 是增强，静默
      }
    })();
  }, POLL_INTERVAL_MS);

  return () => {
    stopped = true;
    clearInterval(timer);
  };
}
