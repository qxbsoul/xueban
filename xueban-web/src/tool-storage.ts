export interface ToolSettingsState {
  tavilyKey: string;
  serpapiKey: string;
  githubToken: string;
}

const KEY = 'sf_tool_settings';

export function loadToolSettings(): ToolSettingsState {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<ToolSettingsState>;
    return { tavilyKey: value.tavilyKey ?? '', serpapiKey: value.serpapiKey ?? '', githubToken: value.githubToken ?? '' };
  } catch {
    return { tavilyKey: '', serpapiKey: '', githubToken: '' };
  }
}

export function saveToolSettings(value: ToolSettingsState): void {
  localStorage.setItem(KEY, JSON.stringify({
    tavilyKey: value.tavilyKey.trim(), serpapiKey: value.serpapiKey.trim(), githubToken: value.githubToken.trim(),
  }));
}

export function toolRequestHeaders(): Record<string, string> {
  const value = loadToolSettings();
  return {
    ...(value.tavilyKey ? { 'X-Tavily-Key': value.tavilyKey } : {}),
    ...(value.serpapiKey ? { 'X-Serpapi-Key': value.serpapiKey } : {}),
    ...(value.githubToken ? { 'X-Github-Token': value.githubToken } : {}),
  };
}
