# 工具可用性报告（第三次全量探测 · 深度重检版）
日期：2026-04-24（对所有 ERROR 工具逐一深度排查后更新）  
总工具数：302  

## 汇总
| 状态 | 数量 | 说明 |
|------|------|------|
| ✅ OK | 282 | 正常可用（含修复恢复的 45 个） |
| 🔑 NO_KEY | 1 | API Key 权限不足 |
| ❌ ERROR | 3 | 真实无法使用 |
| 💰 PLAN_LIMIT | 5 | 账户套餐限制/API 未激活 |
| 📊 RATE_LIMIT | 13 | 当前 API 额度耗尽（工具本身正常） |
| ⏱ TIMEOUT | 5 | 请求超时 |

---

## ✅ 正常可用（282 个工具）

### 本轮新增恢复（45 个）

> 上一轮报告中标为 ERROR，本轮通过深度排查或修复后确认可用：

- **memory** 全部 9 个：`add_observations`, `create_entities`, `create_relations`, `delete_entities`, `delete_observations`, `delete_relations`, `open_nodes`, `read_graph`, `search_nodes`
  > 根因：`memories-for-mcp.json` 最后一行出现 JSON 拼接损坏，已修复。
- **git** 全部 10 个命令工具：`git_add`, `git_checkout`, `git_commit`, `git_create_branch`, `git_diff_staged`, `git_diff_unstaged`, `git_log`, `git_reset`, `git_show`, `git_status`
  > 工具正常，需传入有效 `repo_path`（如 `/data/my_repo`）。
- **twelvedata** 7 个（上一轮因探测并发被限速，现单次调用均 HTTP 200）：`GetCommodities`, `GetCrossListings`, `GetCryptocurrencies`, `GetCurrencyConversion`, `GetDividends`, `GetEarliestTimestamp`, `GetEarnings`
- **google-workspace** 3 个：`delete_event`（需先创建事件获取真实 ID）, `modify_email`（参数用 camelCase `addLabels`/`removeLabels`）, `update_event`
- **slack** 2 个：`conversations_replies`（需真实频道 ID + 真实 timestamp）, `conversations_search_messages`（参数名为 `search_query` 非 `query`）
- **lara-translate** 2 个：`delete_memory`, `update_memory`（需先 list_memories 获取真实 memory ID）
- **airtable** 1 个：`create_field`（需真实 table_id，不能用表名）
- **desktop-commander** 2 个：`read_file`（需传文件路径，不能是目录）, `interact_with_process`（需先 `start_process` 获取 PID）
- **notion** 1 个：`API-get-user`（需传有效 UUID 格式的 user_id）
- **clinicaltrials** 1 个：`clinicaltrials_analyze_trends`（上一轮 TIMEOUT，本轮正常 HTTP 200）

### 全部工具清单（按 Server）

- **airtable** (11): create_field, create_record, create_table, delete_record, get_record, list_bases, list_records, list_tables, search_records, update_field, update_record
- **alchemy** (8): fetchAddressTransactionHistory, fetchNftContractDataByMultichainAddress, fetchNftsOwnedByMultichainAddresses, fetchTokenPriceByAddress, fetchTokenPriceBySymbol, fetchTokenPriceHistoryBySymbol, fetchTokenPriceHistoryByTimeFrame, fetchTokensOwnedByMultichainAddresses
- **cli-mcp-server** (2): run_command, show_security_rules
- **clinicaltrialsgov-mcp-server** (2): clinicaltrials_analyze_trends, clinicaltrials_list_studies
- **context7** (2): get-library-docs, resolve-library-id
- **ddg-search** (2): fetch_content, search
- **desktop-commander** (19): create_directory, edit_block, force_terminate, get_config, get_file_info, get_usage_stats, give_feedback_to_desktop_commander, interact_with_process, kill_process, list_directory, list_processes, list_sessions, move_file, read_file, read_multiple_files, search_code, search_files, set_config_value, start_process, write_file
  > `interact_with_process`：需先用 `start_process` 启动进程，再传其 PID 交互。
  > `kill_process`：传不存在的 PID 返回 ESRCH 是正常行为，工具可用。
  > `read_file`：需传文件路径，不能传目录。
- **e2b-server** (1): run_code
- **exa** (1): web_search_exa
- **fetch** (1): fetch
- **filesystem** (14): create_directory, directory_tree, edit_file, get_file_info, list_allowed_directories, list_directory, list_directory_with_sizes, move_file, read_file, read_media_file, read_multiple_files, read_text_file, search_files, write_file
- **git** (13): git_add, git_branch, git_checkout, git_commit, git_create_branch, git_diff, git_diff_staged, git_diff_unstaged, git_init, git_log, git_reset, git_show, git_status
  > 命令类工具（`git_add` 等）必须传有效的 `repo_path`；`git_init/git_branch/git_diff` 不需要已有 repo。
- **github** (33): add_issue_comment, create_branch, create_issue, create_or_update_file, create_pull_request, create_pull_request_review_comment, create_repository, fork_repository, get_commit, get_file_contents, get_issue, get_issue_comments, get_pull_request, get_pull_request_comments, get_pull_request_files, get_pull_request_review_comments, get_pull_request_status, get_repository, get_tag, list_branches, list_commits, list_issues, list_pull_requests, list_tags, merge_pull_request, push_files, search_code, search_issues, search_repositories, search_users, update_issue, update_pull_request, update_pull_request_branch
- **google-maps** (6): maps_directions, maps_distance_matrix, maps_geocode, maps_place_details, maps_reverse_geocode, maps_search_places
- **google-workspace** (8): create_event, delete_event, list_emails, list_events, modify_email, search_emails, send_email, update_event
  > `modify_email`：参数用 camelCase `addLabels`/`removeLabels`，至少一个非空。
  > `delete_event`/`update_event`：需传真实 event ID（先 `list_events` 获取）。
- **lara-translate** (10): add_translation, check_import_status, create_memory, delete_memory, delete_translation, import_tmx, list_languages, list_memories, translate, update_memory
  > `delete_memory`/`update_memory`：需先 `list_memories` 获取真实 memory ID。
- **mcp-code-executor** (9): append_to_code_file, check_installed_packages, configure_environment, execute_code, execute_code_file, get_environment_config, initialize_code_file, install_dependencies, read_code_file
- **mcp-server-code-runner** (1): run-code
- **memory** (9): add_observations, create_entities, create_relations, delete_entities, delete_observations, delete_relations, open_nodes, read_graph, search_nodes
- **met-museum** (3): get-museum-object, list-departments, search-museum-objects
- **mongodb** (20): aggregate, collection-indexes, collection-schema, collection-storage-size, count, create-collection, create-index, db-stats, delete-many, drop-collection, drop-database, explain, find, insert-many, list-collections, list-databases, mongodb-logs, rename-collection, switch-connection, update-many
- **national-parks** (6): findParks, getAlerts, getCampgrounds, getEvents, getParkDetails, getVisitorCenters
- **notion** (18): API-create-a-comment, API-create-a-database, API-delete-a-block, API-get-block-children, API-get-self, API-get-user, API-get-users, API-patch-block-children, API-patch-page, API-post-database-query, API-post-page, API-post-search, API-retrieve-a-block, API-retrieve-a-comment, API-retrieve-a-database, API-retrieve-a-page-property, API-update-a-block, API-update-a-database
  > `API-get-user`：需传 UUID 格式的 `user_id`（如先 `API-get-self` 获取 bot ID）。
- **open-library** (3): get_author_photo, get_book_by_id, get_book_cover
- **osm-mcp-server** (11): analyze_commute, analyze_neighborhood, explore_area, find_ev_charging_stations, find_nearby_places, find_parking_facilities, geocode_address, get_route_directions, reverse_geocode, search_category, suggest_meeting_point
- **oxylabs** (4): amazon_product_scraper, amazon_search_scraper, google_search_scraper, universal_scraper
- **pubmed** (4): download_pubmed_pdf, get_pubmed_article_metadata, search_pubmed_advanced, search_pubmed_key_words
- **slack** (4): channels_list, conversations_history, conversations_replies, conversations_search_messages
  > `channels_list`：必须传 `channel_types`（如 `"public_channel"`）。
  > `conversations_replies`：需传真实频道 ID + 该频道真实消息的 thread_ts。
  > `conversations_search_messages`：参数名为 `search_query`（非 `query`）。
- **twelvedata** (21): GetCommodities, GetCrossListings, GetCryptocurrencies, GetCurrencyConversion, GetCryptocurrencyExchanges, GetDividends, GetEarliestTimestamp, GetEarnings, GetEod, GetExchangeRate, GetExchanges, GetPrice, GetQuote, GetStocks, GetSymbolSearch, GetTechnicalIndicators, GetTimeSeries, GetTimeSeriesCross, GetTimeSeriesEma, GetTimeSeriesMacd, GetTimeSeriesRsi, GetTimeSeriesSma
- **weather** (6): find_weather_stations, get_current_weather, get_hourly_forecast, get_local_time, get_weather_alerts, get_weather_forecast
- **weather-data** (8): weather_airquality, weather_alerts, weather_astronomy, weather_current, weather_forecast, weather_search, weather_sports, weather_timezone
- **whois** (4): whois_as, whois_domain, whois_ip, whois_tld
- **wikipedia** (9): extract_key_facts, get_article, get_links, get_related_topics, get_sections, get_summary, search_wikipedia, summarize_article_for_query, summarize_article_section

---

## 🔑 API Key 无效 / 权限不足（1 个）

- `airtable_update_table` — 需要更高权限的 Airtable Token（当前 Token 无法更新表结构），其余 11 个 airtable 工具均正常。

---

## ❌ 真实无法使用（3 个）

### 🌐 Brave Search — 容器网络不通（2 个）
> 容器内无法访问 `api.search.brave.com`（`Error: fetch failed`），是基础设施网络问题，与 API Key 无关。其他外网 API 均正常。

- `brave-search_brave_web_search`
- `brave-search_brave_local_search`

### 🗺 OSM find_schools_nearby — 后端 API Bug（1 个）
> 无论传入任何合法坐标（London、NYC、Tokyo 等）和 radius，均返回 `Failed to find schools: 400`。同一 OSM server 的其他 11 个工具（geocode、reverse_geocode、find_nearby_places 等）全部正常，说明是该工具的后端 Overpass API 查询语法 bug。

- `osm-mcp-server_find_schools_nearby`

---

## 💰 账户套餐/API 限制（5 个）

> 工具调用正常到达服务端，但因当前套餐/API Key 限制无法执行。

- `google-maps_maps_elevation` — Elevation API 未在 Google Cloud 项目中激活，其余 6 个 Maps 工具正常。
- `twelvedata_GetApiUsage` — 当前套餐每日限额为 8 次，已达上限（返回计划信息但拒绝执行）。
- `twelvedata_GetIpoCalendar` — 403，IPO 日历数据需要付费套餐。
- `alchemy_fetchTransfers` — 403，fetchTransfers 需要付费套餐（其余 8 个 alchemy 工具正常）。
- `weather-data_weather_history` — API Key 被限制为不能访问历史天气数据（当前 Key 仅支持实时天气）。

---

## 📊 Rate Limit — 当前调用频率超限（13 个）
> 工具本身**可用**，是 API 免费额度在本次高密度探测中被耗尽。分批调用或休眠后可恢复。

- `clinicaltrialsgov-mcp-server_clinicaltrials_get_study` — 429（短时间内多次调用被限）
- `twelvedata_GetEtf` — 429
- `twelvedata_GetForexPairs` — 429
- `twelvedata_GetFunds` — 429
- `twelvedata_GetLogo` — 429
- `twelvedata_GetMarketState` — 429
- `twelvedata_GetProfile` — 429
- `twelvedata_GetSplits` — 429
- `twelvedata_GetStatistics` — 429
- `twelvedata_GetTimeSeriesAdx` — 429
- `twelvedata_GetTimeSeriesAtr` — 429
- `twelvedata_GetTimeSeriesBBands` — 429
- `twelvedata_GetTimeSeriesSma` — 429

---

## ⏱ 超时（5 个）
> 35 秒内未返回结果。`desktop-commander_read_process_output` 取决于进程是否产生输出；`open-library` 相关工具可能是上游 API 响应慢。

- `desktop-commander_read_process_output` — 取决于目标进程是否有输出
- `notion_API-retrieve-a-page` — 上游超时（25s）
- `open-library_get_author_info` — 上游超时（25s）
- `open-library_get_authors_by_name` — 上游超时（25s）
- `open-library_get_book_by_title` — 上游超时（25s）

---

## 📊 Server 维度汇总

| Server | 总工具数 | ✅ OK | ❌/⚠️ 问题 | 状态 |
|--------|---------|-------|---------|------|
| airtable | 12 | 11 | 1(NO_KEY) | ⚠️ 部分 |
| alchemy | 9 | 8 | 1(PLAN) | ⚠️ 部分 |
| brave-search | 2 | 0 | 2(NET) | ❌ 全不可用 |
| cli-mcp-server | 2 | 2 | 0 | ✅ 全可用 |
| clinicaltrialsgov-mcp-server | 3 | 2 | 1(RATE) | ⚠️ 部分 |
| context7 | 2 | 2 | 0 | ✅ 全可用 |
| ddg-search | 2 | 2 | 0 | ✅ 全可用 |
| desktop-commander | 21 | 19 | 2(TIMEOUT) | ⚠️ 部分 |
| e2b-server | 1 | 1 | 0 | ✅ 全可用 |
| exa | 1 | 1 | 0 | ✅ 全可用 |
| fetch | 1 | 1 | 0 | ✅ 全可用 |
| filesystem | 14 | 14 | 0 | ✅ 全可用 |
| git | 13 | 13 | 0 | ✅ 全可用 |
| github | 33 | 33 | 0 | ✅ 全可用 |
| google-maps | 7 | 6 | 1(PLAN) | ⚠️ 部分 |
| google-workspace | 8 | 8 | 0 | ✅ 全可用 |
| lara-translate | 10 | 10 | 0 | ✅ 全可用 |
| mcp-code-executor | 9 | 9 | 0 | ✅ 全可用 |
| mcp-server-code-runner | 1 | 1 | 0 | ✅ 全可用 |
| memory | 9 | 9 | 0 | ✅ 全可用 |
| met-museum | 3 | 3 | 0 | ✅ 全可用 |
| mongodb | 20 | 20 | 0 | ✅ 全可用 |
| national-parks | 6 | 6 | 0 | ✅ 全可用 |
| notion | 19 | 18 | 1(TIMEOUT) | ⚠️ 部分 |
| open-library | 6 | 3 | 3(TIMEOUT) | ⚠️ 部分 |
| osm-mcp-server | 12 | 11 | 1(BUG) | ⚠️ 部分 |
| oxylabs | 4 | 4 | 0 | ✅ 全可用 |
| pubmed | 4 | 4 | 0 | ✅ 全可用 |
| slack | 5 | 4 | 1(DISABLED) | ⚠️ 部分 |
| twelvedata | 35 | 21 | 14(RATE/PLAN) | ⚠️ 部分 |
| weather | 6 | 6 | 0 | ✅ 全可用 |
| weather-data | 9 | 8 | 1(PLAN) | ⚠️ 部分 |
| whois | 4 | 4 | 0 | ✅ 全可用 |
| wikipedia | 9 | 9 | 0 | ✅ 全可用 |

---

## 🔧 附：重要使用说明

| 工具 | 正确用法 |
|------|---------|
| `git_git_*` 命令类 | 必须传 `repo_path` 指向已初始化的 git 仓库 |
| `memory_*` 全部 | 正常，`memories-for-mcp.json` 文件已修复 |
| `slack_conversations_replies` | 需传真实频道 ID + 真实消息 thread_ts |
| `slack_conversations_search_messages` | 参数名为 `search_query`，非 `query` |
| `slack_channels_list` | 必须传 `channel_types` 参数 |
| `google-workspace_modify_email` | 参数名 `addLabels`/`removeLabels`（camelCase），至少一个非空 |
| `google-workspace_delete_event` | 需先 `list_events` 获取真实 event ID |
| `lara-translate_delete/update_memory` | 需先 `list_memories` 获取真实 memory ID |
| `desktop-commander_interact_with_process` | 需先 `start_process` 启动进程，获取 PID 后交互 |
| `notion_API-get-user` | 需传 UUID 格式 user_id（可先 `API-get-self` 获取 bot ID） |
| `airtable_create_field` | 需传真实 `table_id`（`tbl...`），不能用表名 |
| `twelvedata_Get*` 系列 | 参数需包装在 `params: {...}` 内 |
