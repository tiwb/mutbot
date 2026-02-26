// 统一的 Session 图标组件，供 Tab、SessionList、RpcMenu 等共享
// 使用 Lucide React 动态渲染，支持按名称查找图标

import { icons, type LucideProps } from "lucide-react";

/** kebab-case → PascalCase: "message-square" → "MessageSquare" */
function kebabToPascal(name: string): string {
  return name
    .split("-")
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join("");
}

/** kind → 默认 Lucide 图标名（kebab-case） */
const KIND_FALLBACK: Record<string, string> = {
  agent: "message-square",
  terminal: "terminal",
  document: "file-text",
  guide: "circle-question-mark",
  researcher: "search",
};

/**
 * 根据 Lucide 图标名（kebab-case）渲染图标组件。
 * 找不到时返回 null。
 */
export function renderLucideIcon(
  name: string,
  size = 24,
  color = "currentColor",
  extraProps?: Partial<LucideProps>,
) {
  const pascal = kebabToPascal(name);
  const Icon = icons[pascal as keyof typeof icons];
  if (!Icon) return null;
  return <Icon size={size} color={color} {...extraProps} />;
}

/**
 * 获取 Session 图标。
 *
 * 优先级：iconName（用户自定义/后端声明） > kind 回退 > 默认 message-square
 */
export function getSessionIcon(
  kind: string,
  size = 24,
  color = "currentColor",
  iconName?: string,
) {
  const name = iconName || KIND_FALLBACK[kind] || "message-square";
  const el = renderLucideIcon(name, size, color);
  if (el) return el;
  // 回退到默认图标
  const FallbackIcon = icons.MessageSquare;
  return <FallbackIcon size={size} color={color} />;
}

/** 获取所有可用的 Lucide 图标名（PascalCase） */
export function getAllIconNames(): string[] {
  return Object.keys(icons);
}
