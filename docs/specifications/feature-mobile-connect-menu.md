# 手机连接菜单 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

mutbot 服务器启动时会在 banner 中打印可用的连接地址（含 `mutbot.ai/connect/#host:port` 链接）。但用户需要手动输入 URL 才能在手机上连接。增加一个菜单项，点击后显示各地址的二维码，方便手机扫码连接。

## 设计方案

### 核心设计

在 `SessionList/Header` 全局菜单中添加 **"Mobile Connect"** 菜单项。

**交互流程**：
1. 用户点击 Header 菜单 → 选择 "Mobile Connect"
2. 后端 `execute()` 收集当前服务器的监听地址，过滤掉 `127.0.0.1`
3. 返回自定义 action `"mobile_connect"`，携带地址列表
4. 前端收到后打开一个弹窗，为每个地址显示一个二维码
5. 如果没有非 localhost 地址，弹窗显示配置说明

**后端**（`builtins/menus.py`）：
- 新增 `MobileConnectMenu(Menu)` 类
- `display_category = "SessionList/Header"`，`display_order = "1workspace:2"`（排在 Restart Server 后面）
- `display_icon = "smartphone"`
- `execute()` 复用 `server.py` 的 `_enumerate_ips()` + `_collect_listen_addresses()` 逻辑，收集非 127.0.0.1 的地址
- 返回 `MenuResult(action="mobile_connect", data={"addresses": [{"url": "http://192.168.1.5:8741", "via": "https://mutbot.ai/connect/#192.168.1.5:8741"}]})`
- 如果所有地址都是 127.0.0.1，返回空列表 + 提示信息

**前端**：
- `App.tsx` 的 `handleMenuResult` 新增处理 `action === "mobile_connect"`
- 新建 `MobileConnectDialog.tsx` 组件，接收地址列表，渲染二维码
- 使用 `qrcode.react` 库生成 SVG 二维码（轻量、React 原生）

**无可用地址时的提示**：
- 显示说明文字：服务器当前仅监听 localhost，需要配置 `listen` 为 `0.0.0.0:8741` 才能接受外部连接
- 示例配置：`~/.mutbot/config.json` 中设置 `"listen": ["0.0.0.0:8741"]`
- 或启动参数：`python -m mutbot --listen 0.0.0.0:8741`

### 弹窗 UI 设计

- 半透明遮罩 + 居中白色弹窗（复用 ShortcutEditDialog 的 overlay 模式）
- 标题："Mobile Connect"
- 每个地址一个卡片，包含：
  - 二维码（120x120 SVG）
  - 地址文字，格式类似 banner：`http://192.168.1.5:8741  (via mutbot.ai/connect/#...)`
  - 二维码内容切换：可选择编码 via URL 或直连 URL（类似 banner 同时展示两种地址，用户点击切换二维码内容）
- 默认二维码编码 via URL（`mutbot.ai/connect/#host:port`）
- 多个地址纵向排列
- 点击遮罩或右上角 × 关闭

## 关键参考

### 源码
- `mutbot/src/mutbot/builtins/menus.py` — 现有菜单实现（RestartServerMenu 等在 `SessionList/Header` 分类下）
- `mutbot/src/mutbot/web/server.py:304-340` — `_enumerate_ips()` 和 `_build_banner_lines()` 地址枚举逻辑
- `mutbot/src/mutbot/menu.py` — Menu/MenuItem/MenuResult 基类定义
- `mutbot/frontend/src/components/RpcMenu.tsx` — 前端菜单组件，处理 action 分发
- `mutbot/frontend/src/App.tsx:430-484` — `handleMenuResult` 和 `handleHeaderAction` 处理菜单结果
- `mutbot/frontend/src/panels/SessionListPanel.tsx:381-394` — Header 菜单渲染位置
- `mutbot/frontend/src/mobile/ShortcutEditDialog.tsx` — 弹窗 overlay 模式参考

## 实施步骤清单

- [x] **Task 1**: 安装前端依赖 `qrcode.react`
  - 状态：✅ 已完成

- [x] **Task 2**: 后端 — 新增 `MobileConnectMenu` 菜单类
  - [x] 在 `builtins/menus.py` 中新增菜单类，复用 `server.py` 的地址枚举逻辑
  - [x] 返回 `action="mobile_connect"`，携带 `addresses` 列表（每项含 `url` 和 `via`）
  - 状态：✅ 已完成

- [x] **Task 3**: 前端 — 新建 `MobileConnectDialog.tsx` 弹窗组件
  - [x] overlay + 居中弹窗布局
  - [x] 每个地址渲染二维码（默认 via URL）+ URL 文字 + 切换按钮
  - [x] 无可用地址时显示配置说明
  - 状态：✅ 已完成

- [x] **Task 4**: 前端 — `App.tsx` 集成菜单结果处理
  - [x] `handleMenuResult` 新增 `action === "mobile_connect"` 分支，打开弹窗
  - [x] 桌面和移动端布局均渲染 MobileConnectDialog
  - 状态：✅ 已完成

- [x] **Task 5**: 构建前端并验证
  - 状态：✅ 已完成
