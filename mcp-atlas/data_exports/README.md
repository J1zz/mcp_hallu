# MCP 服务器测试数据导出

本目录包含可上传至各类在线服务的示例数据，用于创建与现有测试提示和评估所使用的状态一致的测试环境。

## 目的

大多数 MCP 服务器连接外部服务时是相对无状态的。
但有 5 个 MCP 服务器（Airtable、Google Calendar、Notion、MongoDB、Slack）需要：
1. 拥有对应服务的账号
2. 向该账号上传数据
3. 提供 API 密钥或连接字符串以访问数据

若要复现测试结果或针对已知数据状态运行评估，需要使用提供的示例数据配置这些服务。

## 可用的数据导出

| 服务 | 文件 | 描述 |
|------|------|------|
| Airtable | https://airtable.com/appIF9byLfQwdHqE2/shr1KTZOgPl0qQmA8 | 访问该链接，点击"Copy base"按钮克隆数据库 |
| Google Calendar | `calendar_mcp_eval_export.zip` | 示例日历事件（解压为 .ics 文件，8KB） |
| Notion | `notion_mcp_eval_export.zip` | 示例页面和数据库（13MB） |
| MongoDB | `mongo_dump_video_game_store.zip` | 示例电子游戏商店数据库（解压为文件夹，486KB） |
| Slack | `slack_mcp_eval_export.zip` | 示例工作区数据（27KB），消息时间戳为 2025 年 12 月初（Slack 免费账号仅显示最近 90 天的消息） |

## 配置说明

以下配置步骤会生成需要填入 `.env` 文件的 API 密钥。

### Airtable
注册 Airtable 账号，访问 [https://airtable.com/appIF9byLfQwdHqE2/shr1KTZOgPl0qQmA8](https://airtable.com/appIF9byLfQwdHqE2/shr1KTZOgPl0qQmA8)，点击"Copy base"克隆数据库。然后获取 API 密钥，并在 `.env` 文件中设置 `AIRTABLE_API_KEY`。

### Google Calendar（google-workspace）
解压 `calendar_mcp_eval_export.zip`，其中包含一个 `.ics` 文件。登录 Google 账号（建议使用新账号，因为会导入日历事件），访问 [https://calendar.google.com/calendar/u/0/r/settings/export](https://calendar.google.com/calendar/u/0/r/settings/export)（确保使用正确的 Google 账号），导入 `.ics` 文件。若要获取 `google-workspace` MCP 服务器所需的 `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_REFRESH_TOKEN`，请参阅 [https://github.com/epaproditus/google-workspace-mcp-server](https://github.com/epaproditus/google-workspace-mcp-server?tab=readme-ov-file#prerequisites) 中的"Prerequisites"和"Setup Instructions"。

### Notion
注册 Notion 账号，进入 Settings > Import，导入 `mcp-atlas-notion-data.zip`。导入过程最多需要几分钟，将上传 6 张数据表和 1 个页面。几分钟后确认所有 6 张表均有数据（Notion 异步加载数据）。若某张表为空，请删除该页面并重新上传对应的单个 CSV 文件。接着访问 [https://www.notion.so/profile/integrations](https://www.notion.so/profile/integrations)，新建一个集成（类型选择 Internal），获取 `Internal Integration Secret`，并在 `.env` 文件中保存为 `NOTION_TOKEN`。

### MongoDB
注册 MongoDB 账号，获取 MongoDB 连接 URI，解压 `mongo_dump_video_game_store.zip`。然后通过如下命令上传数据：`mongorestore --uri="mongodb+srv://<username>:<password>@<cluster-url>" mongo_dump_video_game_store`。如果尚未安装 MongoDB CLI，请参考 [安装文档](https://www.mongodb.com/docs/mongocli/current/install/)。将连接 URI 在 `.env` 文件中保存为 `MONGODB_CONNECTION_STRING`。

### Slack
创建一个新的 Slack 工作区，访问 `https://<你的工作区名称>.slack.com/services/import`，导入 `slack_mcp_eval_export.zip`。注意：Slack 免费账号有 90 天的消息限制，超过 90 天的消息将不可见。你可以修改导出文件中的时间戳使其更新，或使用每月 9 美元的付费 Slack 账号。获取 `SLACK_MCP_XOXC_TOKEN` 和 `SLACK_MCP_XOXD_TOKEN` 请参考：[https://github.com/korotovsky/slack-mcp-server/blob/HEAD/docs/01-authentication-setup.md](https://github.com/korotovsky/slack-mcp-server/blob/HEAD/docs/01-authentication-setup.md)

## 注意

如果没有这些示例数据，MCP 服务器仍然可以正常运行，但当测试提示引用了你账号中不存在的特定数据时，可能会返回空结果或报错。
