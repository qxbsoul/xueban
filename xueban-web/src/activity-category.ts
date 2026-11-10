/** 数据流分类：决定时间线左侧细线的颜色。 */
export type ActivityCategory = 'web' | 'knowledge' | 'model' | 'approval' | 'task' | 'tool';

const LABELS: Record<ActivityCategory, string> = {
  web: '联网搜索',
  knowledge: '知识库',
  model: '模型',
  approval: '人工审批',
  task: '任务流向',
  tool: '工具',
};

export function activityCategory(activity: { kind?: string; name?: string; title?: string }): ActivityCategory {
  const text = `${activity.name ?? ''} ${activity.title ?? ''}`.toLowerCase();
  if (/web|搜索|联网|search|browse/.test(text)) return 'web';
  if (/knowledge|课程资源|记忆库|memory|rag/.test(text)) return 'knowledge';
  if (activity.kind === 'approval' || /approval|审批/.test(text)) return 'approval';
  if (activity.kind === 'dispatch') return 'task';
  if (activity.kind === 'model') return 'model';
  return 'tool';
}

export function activityCategoryLabel(category: ActivityCategory): string {
  return LABELS[category];
}

const TOOL_LABELS: Record<string, string> = {
  web_search: '联网搜索',
  knowledge_search: '检索课程资源库',
  search_knowledge: '检索课程资源库',
  read_file: '读取文件',
  write_file: '写入文件',
  list_files: '浏览文件',
  run_shell: '执行命令',
  http_request: '请求网页',
};

/** 数据流里展示工具的中文名，不把 web_search 这种内部名暴露给学生。 */
export function toolDisplayName(name: string | undefined): string {
  if (!name) return '工具调用';
  return TOOL_LABELS[name] ?? name.replace(/_/g, ' ');
}
