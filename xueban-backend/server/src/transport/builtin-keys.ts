/**
 * 内置模型密钥：用户没有在「设置 → 模型与 API」里配置个人 Key 时，
 * 服务端用这里的 Key 兜底（环境变量 MIMO_API_KEY 优先级更高）。
 *
 * 换 Key 只改这里；它不会下发给浏览器，只在 Node 进程内使用。
 */
export const BUILTIN_MIMO_API_KEY = 'sk-cbpvnspi0m8803fea1r54ths266jpmlmx0jufezfkkmz9kph';
