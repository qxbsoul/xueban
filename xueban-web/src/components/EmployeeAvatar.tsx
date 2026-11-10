import { CSSProperties } from 'react';
import { avatarIdFor, avatarSrc, isAvatarId } from '../avatar-set';

interface EmployeeAvatarProps {
  name: string;
  /** 教研员/岗位显式选中的 micah 头像 id；历史单字头像会自动按名字映射。 */
  avatar?: string;
  size?: number;
  /** 右下角在岗状态点。 */
  dot?: boolean;
  className?: string;
}

/**
 * 教研员头像：统一使用 DiceBear「micah」插画脸。
 * 没有显式头像时按名字哈希，保证同一个人到处长一样。
 */
export default function EmployeeAvatar({ name, avatar, size = 34, dot = false, className = '' }: EmployeeAvatarProps) {
  const id = isAvatarId(avatar) ? avatar : avatarIdFor(name);
  const style = {
    '--tone': '#ff5e3a',
    width: size,
    height: size,
    borderRadius: Math.max(7, Math.round(size * 0.3)),
  } as CSSProperties;
  return (
    <span
      className={`emp-avatar face${className ? ` ${className}` : ''}`}
      style={style}
      aria-hidden="true"
    >
      <img src={avatarSrc(id)} alt="" loading="lazy" draggable={false} />
      {dot && <i />}
    </span>
  );
}
