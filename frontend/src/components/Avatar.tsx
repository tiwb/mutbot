/**
 * Avatar — 圆形头像组件，支持图片和首字母缩写两种模式。
 */

interface AvatarProps {
  name: string;
  avatar?: string;
  size?: number;
}

/** 从名称生成稳定的 HSL 色相。 */
function nameToHue(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  return ((hash % 360) + 360) % 360;
}

export default function Avatar({ name, avatar, size = 32 }: AvatarProps) {
  const initials = name.charAt(0).toUpperCase();
  const hue = nameToHue(name);
  const borderColor = `hsl(${hue}, 55%, 55%)`;

  if (avatar) {
    return (
      <div
        className="avatar"
        style={{ width: size, height: size }}
      >
        <img src={avatar} alt={name} className="avatar-img" />
      </div>
    );
  }

  return (
    <div
      className="avatar avatar-initials"
      style={{
        width: size,
        height: size,
        borderColor,
        color: borderColor,
        fontSize: size * 0.45,
      }}
      title={name}
    >
      {initials}
    </div>
  );
}
