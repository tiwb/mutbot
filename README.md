# mutbot

基于 [mutagent](https://github.com/tiwb/mutagent) 的 Web 应用，提供 Workspace/Session 管理、Agent 对话、终端集成、文件编辑等功能。

> **Note:** 早期开发阶段。

## 快速开始

```bash
pip install mutbot
python -m mutbot
```

启动后访问 http://localhost:8741。

```
python -m mutbot --host 0.0.0.0 --port 8741   # 远程访问模式
```

## 前端开发

```bash
cd frontend && npm install && npm run dev      # HMR 开发
npm run build                                   # 生产构建 → src/mutbot/web/frontend_dist/
```

## 技术栈

- **后端**：FastAPI + uvicorn
- **前端**：React 19 + flexlayout-react + xterm.js + Monaco Editor
- **通信**：WebSocket（Agent 事件流 + 终端 I/O）

## 设计文档

详见 [docs/specifications/](docs/specifications/) 目录。

## License

MIT
