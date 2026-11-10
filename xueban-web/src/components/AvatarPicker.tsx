import { AVATAR_IDS, avatarSrc } from '../avatar-set';

interface AvatarPickerProps {
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  label?: string;
}

/** 新建教研员 / 岗位编辑时的头像选择器：12 张 micah 插画脸。 */
export default function AvatarPicker({ value, onChange, disabled = false, label = '选择头像' }: AvatarPickerProps) {
  return (
    <div className="form-group">
      <label className="form-label">{label}</label>
      <div className="avatar-picker" role="radiogroup" aria-label={label}>
        {AVATAR_IDS.map(id => (
          <button
            key={id}
            type="button"
            role="radio"
            aria-checked={value === id}
            aria-label={id}
            className={'avatar-choice' + (value === id ? ' selected' : '')}
            disabled={disabled}
            onClick={() => onChange(id)}
          >
            <img src={avatarSrc(id)} alt="" loading="lazy" draggable={false} />
          </button>
        ))}
      </div>
    </div>
  );
}
