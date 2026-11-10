/* 纯 UI 常量与本地标识工具；业务数据只来自后端。 */

export const uid = (p: string) => p + '-' + Math.random().toString(36).slice(2, 8);

export const AVATAR_PICKS = ['总', '客', '策', '文', '设', '审', '数', '研'];
export const KB_TYPES = ['全部', '知识包', '已就绪', '待索引', '索引中', '失败'];
