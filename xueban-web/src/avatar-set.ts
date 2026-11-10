/** DiceBear「micah」头像集：本地静态资源，避免运行时依赖外网。 */
export const AVATAR_IDS = [
  'micah-01', 'micah-02', 'micah-03', 'micah-04', 'micah-05', 'micah-06',
  'micah-07', 'micah-08', 'micah-09', 'micah-10', 'micah-11', 'micah-12',
] as const;

export type AvatarId = typeof AVATAR_IDS[number];

export const avatarSrc = (id: string): string => `/avatars/${id}.svg`;

export function isAvatarId(value?: string | null): value is AvatarId {
  return typeof value === 'string' && (AVATAR_IDS as readonly string[]).includes(value);
}

function hashOf(text: string): number {
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) | 0;
  }
  return Math.abs(hash);
}

/** 没有显式选过头像的教研员（含历史数据）按名字稳定映射到同一张脸。 */
export function avatarIdFor(name: string): AvatarId {
  return AVATAR_IDS[hashOf(name || '教研员') % AVATAR_IDS.length];
}
