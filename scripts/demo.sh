#!/usr/bin/env bash
# Guided incident demonstration.
#
#   ./scripts/demo.sh                 run the default cpu_spike walkthrough
#   ./scripts/demo.sh crash_loop      run a different scenario
#   ./scripts/demo.sh error_burst svc-inventory-api
#
# Narrates each step so it can be driven live in front of an audience. Every
# number printed is read back from the running API - nothing here is scripted
# output.

set -euo pipefail

BASE="${CLOUDOPS_URL:-http://localhost:8000}"
SCENARIO="${1:-cpu_spike}"
TARGET="${2:-svc-checkout-api}"
DURATION="${3:-180}"
AUTH=()
[ -n "${CLOUDOPS_API_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${CLOUDOPS_API_TOKEN}")

B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[0m'

say()  { printf '\n%s==> %s%s\n' "$B" "$1" "$R"; }
note() { printf '%s    %s%s\n' "$D" "$1" "$R"; }

jq_py() { python -c "import sys,json;d=json.load(sys.stdin);$1"; }

require() {
  if ! curl -sf "${BASE}/readyz" >/dev/null; then
    printf '%sControl plane is not ready at %s%s\n' "$Y" "$BASE" "$R"
    printf 'Start it with:  docker compose up -d --build\n'
    exit 1
  fi
}

require

say "Step 1 - the estate before anything happens"
curl -s "${AUTH[@]}" "${BASE}/api/v1/overview" | jq_py '
print(f"    resources        {d[\"fleet\"][\"total\"]}  ({d[\"fleet\"][\"counts\"]})")
print(f"    fleet health     {d[\"fleet\"][\"score\"]}  ({d[\"fleet\"][\"status\"]})")
print(f"    monthly spend    ${d[\"cost\"][\"total_monthly\"]:,.2f}")
print(f"    identified waste ${d[\"cost\"][\"waste_monthly\"]:,.2f}  ({d[\"cost\"][\"waste_pct\"]}%)")
print(f"    savings found    ${d[\"recommendations\"][\"summary\"][\"monthly_saving\"]:,.2f}/mo")
print(f"    firing alerts    {d[\"alerts\"][\"summary\"][\"firing\"]}")'

say "Step 2 - the top cost recommendations"
curl -s "${AUTH[@]}" "${BASE}/api/v1/recommendations" | jq_py '
rows=[r for r in d["recommendations"] if r["monthly_saving"]>0][:4]
[print(f"    ${r[\"monthly_saving\"]:>9,.2f}/mo  {r[\"title\"]}") for r in rows]'

say "Step 3 - injecting a REAL incident: ${SCENARIO} on ${TARGET}"
note "For a live container this calls its chaos endpoint. The container"
note "genuinely misbehaves; the detection path is told nothing."
curl -s -X POST "${AUTH[@]}" -H 'content-type: application/json' \
  -d "{\"scenario\":\"${SCENARIO}\",\"resource_id\":\"${TARGET}\",\"duration_seconds\":${DURATION}}" \
  "${BASE}/api/v1/incidents" | jq_py '
print(f"    incident      {d[\"id\"]}")
print(f"    scenario      {d[\"params\"][\"label\"]}")
print(f"    target kind   {d[\"params\"][\"target_kind\"]}")
print(f"    chaos in pod  {d[\"params\"].get(\"chaos_injected\", False)}")'

say "Step 4 - watching the pipeline react (90 seconds)"
for i in $(seq 1 9); do
  sleep 10
  curl -s "${AUTH[@]}" "${BASE}/api/v1/overview" | jq_py "
r=[x for x in d['resources'] if x['id']=='${TARGET}'][0]
m=r['metrics']
print(f\"    t+$((i*10))s  cpu={m.get('cpu_utilization',0):5.1f}%  mem={m.get('memory_utilization',0):5.1f}%  \"
      f\"err={m.get('error_rate',0)*100:5.2f}%  p95={m.get('latency_p95_ms',0):6.0f}ms  \"
      f\"restarts={m.get('restart_count',0):.0f}  health={r['score']:3d} ({r['status']})  \"
      f\"alerts={d['alerts']['summary']['firing']}\")"
done

say "Step 5 - what the platform worked out by itself"
curl -s "${AUTH[@]}" "${BASE}/api/v1/alerts?status=firing" | jq_py '
print(f"    {d[\"count\"]} firing alert(s)")
[print(f"      [{a[\"severity\"]:8}] {a[\"summary\"]}  -> {a[\"runbook\"]}") for a in d["alerts"]]'
curl -s "${AUTH[@]}" "${BASE}/api/v1/anomalies?minutes=10" | jq_py '
print(f"    {d[\"count\"]} anomaly detection(s)")
[print(f"      {a[\"resource_id\"]:20} {a[\"metric\"]:20} {a[\"value\"]:>9.2f} vs baseline {a[\"baseline\"]:>8.2f}  score {a[\"score\"]}  ({a[\"method\"]})") for a in d["anomalies"][:6]]'

say "Step 6 - the error-level log lines it produced"
curl -s "${AUTH[@]}" "${BASE}/api/v1/logs?minutes=10&level=ERROR&limit=6" | jq_py '
[print(f"      {l[\"service\"]:18} {l[\"message\"][:80]}") for l in d["logs"]]'

say "Step 7 - clearing the incident"
curl -s -X DELETE "${AUTH[@]}" "${BASE}/api/v1/incidents" | jq_py 'print(f"    cancelled {d[\"cancelled\"]} incident(s)")'
note "Alerts resolve on their own once the condition has been clear for 120s."
printf '\n%sDone. Dashboard: %s%s\n\n' "$G" "$BASE" "$R"
