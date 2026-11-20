#!/usr/bin/env bash
# deepseek-search.sh — DeepSeek Search / Fetch API CLI (bash 3.2 compatible)
# Usage: deepseek-search.sh search <query> [options]
#        deepseek-search.sh fetch <url> [options]
#        deepseek-search.sh --help

set -u

# ---------- globals ----------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE_URL="https://api.deepseek.com/v1/tools"

KEYFILE="$SKILL_ROOT/DeepSeek_API_Key"
KEY=""
if [ -f "$KEYFILE" ]; then
  KEY="$(tr -d '\r\n' < "$KEYFILE")"
fi

# ---------- helpers ----------
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

need_deps() {
  command -v curl >/dev/null 2>&1 || die "curl not found"
  command -v jq   >/dev/null 2>&1 || die "jq not found (install with: brew install jq)"
  [ -n "$KEY" ] || die "没有密钥配置：未找到 ${KEYFILE}，请在该路径放置 DeepSeek_API_Key 文件"
}

show_help() {
  cat <<'EOF'
deepseek-search.sh — DeepSeek Search / Fetch API CLI

USAGE:
  deepseek-search.sh search <query> [options]
  deepseek-search.sh fetch <url> [options]
  deepseek-search.sh --help

SEARCH OPTIONS (request fields):
  --count N             Number of results, 1-50 (default: 10)
  --ori-question TEXT   Original user question for better intent (1-128 chars)
  --localization IP     IP address for localized results
  --need-content        Include full webpage content in results (default: false)
  --json                Output raw JSON instead of formatted text

SEARCH SYNTAX FLAGS (auto-appended to query; all optional, repeatable where noted):
  --site DOMAIN         site:DOMAIN          (repeatable; multiple joined with OR)
  --exclude-site DOM    -site:DOM            (repeatable)
  --exclude KEYWORD     -KEYWORD             (repeatable)
  --exclude-phrase STR  -"STR"               (repeatable)
  --phrase STR          "STR"                (repeatable)
  --lang LANGS          lang:LANGS           (e.g. zh,en)
  --after DATE          after:DATE           (e.g. 2025 or 2025-01-01)
  --before DATE         before:DATE

  You can also write operators directly inside <query>, e.g.:
    "DeepSeek site:github.com OR site:huggingface.co lang:zh after:2025"

FETCH OPTIONS:
  --json                Output raw JSON instead of formatted text

EXAMPLES:
  deepseek-search.sh search "DeepSeek V4 发布" --count 5 --need-content
  deepseek-search.sh search "模型" --site github.com --lang zh,en --after 2025
  deepseek-search.sh fetch "https://example.com/article"

RESULT HINTS:
  rerank_score >= 1.5  = good result
  authority    >= 3    = highly authoritative

KEY:
  Reads DeepSeek_API_Key from skill root directory (parent of scripts/).
  Missing file => error: 没有密钥配置.
EOF
}

# ---------- arg parsing ----------
[ $# -gt 0 ] || { show_help; exit 1; }
case "$1" in
  -h|--help|help) show_help; exit 0 ;;
  search|fetch) CMD="$1"; shift ;;
  *) die "unknown command: $1 (use 'search' or 'fetch')" ;;
esac

[ $# -gt 0 ] || die "missing argument for $CMD"
POSITIONAL="$1"; shift

COUNT=10
ORI_QUESTION=""
LOCALIZATION=""
NEED_CONTENT=false
JSON_OUT=false
Q_SITES=""
Q_EXCLUDE_SITES=""
Q_EXCLUDES=""
Q_EXCLUDE_PHRASES=""
Q_PHRASES=""
Q_LANG=""
Q_AFTER=""
Q_BEFORE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --count)            COUNT="${2:-}"; shift 2 ;;
    --ori-question)     ORI_QUESTION="${2:-}"; shift 2 ;;
    --localization)     LOCALIZATION="${2:-}"; shift 2 ;;
    --need-content)     NEED_CONTENT=true; shift ;;
    --json)             JSON_OUT=true; shift ;;
    --site)             Q_SITES="$Q_SITES ${2:-}"; shift 2 ;;
    --exclude-site)     Q_EXCLUDE_SITES="$Q_EXCLUDE_SITES ${2:-}"; shift 2 ;;
    --exclude)          Q_EXCLUDES="$Q_EXCLUDES ${2:-}"; shift 2 ;;
    --exclude-phrase)   Q_EXCLUDE_PHRASES="${Q_EXCLUDE_PHRASES}"$'\n'"${2:-}"; shift 2 ;;
    --phrase)           Q_PHRASES="${Q_PHRASES}"$'\n'"${2:-}"; shift 2 ;;
    --lang)             Q_LANG="${2:-}"; shift 2 ;;
    --after)            Q_AFTER="${2:-}"; shift 2 ;;
    --before)           Q_BEFORE="${2:-}"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done

# ---------- validation ----------
need_deps

case "$COUNT" in
  ''|*[!0-9]*) die "--count must be an integer" ;;
esac
if [ "$COUNT" -lt 1 ] || [ "$COUNT" -gt 50 ]; then
  die "--count must be between 1 and 50"
fi

if [ -z "$(printf '%s' "$POSITIONAL" | tr -d '[:space:]')" ]; then
  die "$( [ "$CMD" = search ] && echo 'query cannot be empty' || echo 'url cannot be empty' )"
fi

if [ "$CMD" = fetch ]; then
  case "$POSITIONAL" in
    http://*|https://*) ;;
    *) die "url must start with http:// or https://" ;;
  esac
  [ ${#POSITIONAL} -le 8192 ] || die "url too long (max 8192)"
  # must contain a host and must not carry userinfo (user:password@)
  url_authority="${POSITIONAL#*://}"
  url_authority="${url_authority%%/*}"
  case "$url_authority" in
    '')  die "url must contain a host" ;;
    *@*) die "url must not carry username/password" ;;
  esac
fi

if [ -n "$ORI_QUESTION" ]; then
  [ ${#ORI_QUESTION} -le 128 ] || die "--ori-question max 128 chars"
fi

# ---------- build query ----------
QUERY="$POSITIONAL"

# --site: repeatable, multiple joined with OR
SITE_PARTS=""
for s in $Q_SITES; do
  [ -z "$s" ] && continue
  if [ -z "$SITE_PARTS" ]; then
    SITE_PARTS="site:$s"
  else
    SITE_PARTS="$SITE_PARTS OR site:$s"
  fi
done
[ -n "$SITE_PARTS" ] && QUERY="$QUERY $SITE_PARTS"

for s in $Q_EXCLUDE_SITES; do
  [ -z "$s" ] && continue
  QUERY="$QUERY -site:$s"
done
for s in $Q_EXCLUDES; do
  [ -z "$s" ] && continue
  QUERY="$QUERY -$s"
done
# phrases may contain spaces — use eval-safe loop with IFS
OLD_IFS="$IFS"
IFS='
'
for s in $Q_EXCLUDE_PHRASES; do
  [ -z "$s" ] && continue
  QUERY="$QUERY -\"$s\""
done
for s in $Q_PHRASES; do
  [ -z "$s" ] && continue
  QUERY="$QUERY \"$s\""
done
IFS="$OLD_IFS"

[ -n "$Q_LANG" ]   && QUERY="$QUERY lang:$Q_LANG"
[ -n "$Q_AFTER" ]  && QUERY="$QUERY after:$Q_AFTER"
[ -n "$Q_BEFORE" ] && QUERY="$QUERY before:$Q_BEFORE"

[ ${#QUERY} -le 128 ] || die "final query exceeds 128 Unicode chars (got ${#QUERY})"

# ---------- build JSON payload with jq ----------
if [ "$CMD" = search ]; then
  if [ -n "$ORI_QUESTION" ]; then
    PAYLOAD="$(jq -nc \
      --arg query "$QUERY" \
      --argjson count "$COUNT" \
      --arg ori_question "$ORI_QUESTION" \
      --arg localization "$LOCALIZATION" \
      --argjson need_content "$NEED_CONTENT" \
      '{query:$query, count:$count, ori_question:$ori_question, localization:$localization, need_content:$need_content}')"
  else
    PAYLOAD="$(jq -nc \
      --arg query "$QUERY" \
      --argjson count "$COUNT" \
      --arg localization "$LOCALIZATION" \
      --argjson need_content "$NEED_CONTENT" \
      '{query:$query, count:$count, localization:$localization, need_content:$need_content}')"
  fi
  ENDPOINT="$BASE_URL/search"
else
  PAYLOAD="$(jq -nc --arg url "$POSITIONAL" '{url:$url}')"
  ENDPOINT="$BASE_URL/fetch"
fi

# ---------- HTTP request with retry ----------
HTTP_BODY=""
HTTP_CODE=""
for attempt in 1 2 3; do
  RESP="$(curl -sS --max-time 30 \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -w $'\n%{http_code}' \
    -d "$PAYLOAD" \
    "$ENDPOINT" 2>&1)" || {
      # network-level curl error
      if [ "$attempt" -lt 3 ]; then
        sleep $((attempt * 2))
        continue
      fi
      printf 'ERROR: curl failed: %s\n' "$RESP" >&2
      exit 1
    }

  HTTP_CODE="${RESP##*$'\n'}"
  HTTP_BODY="${RESP%$'\n'*}"

  case "$HTTP_CODE" in
    408|429|5??)
      if [ "$attempt" -lt 3 ]; then
        sleep $((attempt * 2))
        continue
      fi
      ;;
    *) break ;;
  esac
done

# ---------- error handling ----------
if [ "$HTTP_CODE" != "200" ]; then
  MSG="$(printf '%s' "$HTTP_BODY" | jq -r '.error.message // empty' 2>/dev/null)"
  ERRCODE="$(printf '%s' "$HTTP_BODY" | jq -r '.error.code // empty' 2>/dev/null)"
  case "$HTTP_CODE" in
    400) die "bad request${MSG:+: $MSG} (check params and JSON)" ;;
    401) die "invalid API key${MSG:+: $MSG}" ;;
    402) die "insufficient balance — please top up your DeepSeek account" ;;
    403) die "fetch blocked by security policy${MSG:+: $MSG}" ;;
    404) die "no fetchable content${MSG:+: $MSG}" ;;
    408) die "request body timeout after 3 attempts" ;;
    413) die "request body too large" ;;
    429) die "rate limited after 3 attempts — wait and retry" ;;
    5??) die "server error (HTTP $HTTP_CODE) after 3 attempts${MSG:+: $MSG}" ;;
    *)   die "HTTP $HTTP_CODE${MSG:+: $MSG}" ;;
  esac
fi

# ---------- response formatting ----------
if [ "$JSON_OUT" = true ]; then
  printf '%s\n' "$HTTP_BODY" | jq .
  exit 0
fi

if [ "$CMD" = search ]; then
  COUNT_N="$(printf '%s' "$HTTP_BODY" | jq '.results | length')"
  if [ "$COUNT_N" -eq 0 ]; then
    echo "No matching results."
    exit 0
  fi
  printf '%s\n' "$HTTP_BODY" | jq -r '
    .results | to_entries[] |
    "[\(.key + 1)] \(.value.title)\n" +
    "    URL:       \(.value.url)\n" +
    "    Score:     \(.value.rerank_score)   Authority: \(.value.authority)   Date: \(.value.date // "unknown")\n" +
    "    Site:      \(.value.site_name // "unknown")\n" +
    "    Icon:      \(.value.site_icon // "-")\n" +
    "    Summary:   \((.value.summary // .value.snippet // "") | if length > 400 then .[0:400] + "…" else . end)\n" +
    (if (.value.content // "") | length > 0 then
      "    Content:   \(.value.content | .[0:300])\(if (.value.content | length) > 300 then "…" else "" end)\n"
     else "" end)
  '
else
  printf '%s\n' "$HTTP_BODY" | jq -r '
    if (.title // "") != "" then "# \(.title)\n\n" else "" end
    + (.content // "")
  '
fi
