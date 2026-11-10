# WebUI 版本记录

WebUI 独立于 Agent / Web 服务版本。本次仅修改 `src/sphere_agent/web/`；服务返回的 `console_version` 为旧版兼容字段，不覆盖页面版本号。

## v3.0.0 · 2026-11-10

- 参考本地示例重做控制台：深色数据看板、浅色柔和界面，统一蓝色操作色和语义状态色。
- 首次访问跟随系统主题，手动切换后保存在当前浏览器。
- 提供运行总览、任务调度、安全账本和实时日志四个视图，以及移动端布局。
- 保留现有 API、WebSocket、任务暂停与恢复、写入确认、急停、事件筛选分页、JSON 查看复制与日志过滤。
- 遥测缺失时显示待观测；零能量显示为零，不再使用满能量占位。
- 使用原生确认对话框、键盘焦点、可访问写入开关与减少动态效果偏好。
- 完整保留 v2.0.0 首页快照；v1.0.0 快照保持原样。
- 按用户要求未运行测试、构建检查或浏览器验收，测试由用户完成。

### 回滚

仓库标签 `webui-v3.0.0` 标记此版本。回滚前端到 v2.0.0，在仓库根目录用 PowerShell 执行：

```powershell
Copy-Item src/sphere_agent/web/index.v2.0.0.html src/sphere_agent/web/index.html
```

v2.0.0 是完整单文件页面，不依赖 v3 的 `app.js` / `console.css`。以上命令不改变服务或其他源码；确认后提交首页修改即可。

恢复 v3.0.0 前端文件：

```powershell
git restore --source webui-v3.0.0 -- src/sphere_agent/web/index.html src/sphere_agent/web/app.js src/sphere_agent/web/console.css
```

也可直接访问 `/index.v2.0.0.html` 或 `/index.v1.0.0.html` 使用旧界面，不改变默认首页。

## v2.0.0

太空 HUD 控制台，快照为 `index.v2.0.0.html`。

## v1.0.0

初始控制台，快照为 `index.v1.0.0.html`。
