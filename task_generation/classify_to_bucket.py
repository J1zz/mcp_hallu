import json
from collections import defaultdict, Counter
from pathlib import Path

TOOLS_PATH = Path("/Users/apple/Desktop/mcp/mcp-atlas/list-tools.json")
OUT_PATH = Path("/Users/apple/Desktop/mcp/mcp-atlas/list-tools_bucketed.json")

bucket_servers = {
    "BASIC": [
        "brave-search", "ddg-search", "exa", "weather", "google-maps", "fetch",
        "whois", "wikipedia", "weather-data", "open-library", "met-museum",
        "national-parks", "oxylabs",
    ],
    "ANALYTICS": [
        "airtable", "mongodb", "f1-mcp-server", "calculator",
        "clinicaltrialsgov-mcp-server", "osm-mcp-server", "memory",
    ],
    "PRODUCTIVITY": [
        "filesystem", "notion", "slack", "google-workspace", "arxiv",
        "pubmed", "desktop-commander", "lara-translate",
    ],
    "FINANCIAL": ["twelvedata", "alchemy"],
    "CODING": [
        "git", "github", "mcp-code-executor", "cli-mcp-server",
        "e2b-server", "context7", "mcp-server-code-runner",
    ],
}

def normalize(name: str) -> str:
    return name.lower()

norm_server_to_bucket = {
    normalize(s): bucket for bucket, servers in bucket_servers.items() for s in servers
}

def classify_tool(name: str) -> str:
    if name in norm_server_to_bucket:
        return norm_server_to_bucket[name]
    for server_norm, bucket in norm_server_to_bucket.items():
        if name.startswith(server_norm):
            return bucket
    return "UNMAPPED"

def main():
    tools = json.loads(TOOLS_PATH.read_text())
    bucket_to_tools = defaultdict(list)
    bucket_counts = Counter()

    for tool in tools:
        tool_name = tool.get("name", "").lower()
        bucket = classify_tool(tool_name)
        tool["bucket"] = bucket  # 在对象里加入 bucket 字段
        bucket_to_tools[bucket].append(tool_name)
        bucket_counts[bucket] += 1

    OUT_PATH.write_text(json.dumps(tools, ensure_ascii=False, indent=2))

    for bucket, names in bucket_to_tools.items():
        print(f"\n[{bucket}] ({len(names)})")
        for n in sorted(names):
            print(f"  - {n}")

    print("\n=== 统计 ===")
    for bucket, cnt in bucket_counts.most_common():
        print(f"{bucket}: {cnt}")

if __name__ == "__main__":
    main()