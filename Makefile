# CloudOps Sentinel
#
#   make up        build and start the stack
#   make verify    prove the deployment actually works
#   make demo      run the guided incident walkthrough
#   make down      stop everything
#
# `make help` lists everything.

SHELL := /bin/bash
COMPOSE := docker compose
URL ?= http://localhost:8000
PY ?= python

.DEFAULT_GOAL := help
.PHONY: help up down build restart logs ps verify verify-quick test lint demo \
        incident stop-incidents observability clean secrets k8s-validate \
        k8s-apply k8s-delete open metrics cost recommendations

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ lifecycle
secrets: ## Generate the local Grafana admin password (never committed)
	@mkdir -p secrets
	@test -s secrets/grafana_admin_password.txt \
	  || $(PY) -c "import secrets;open('secrets/grafana_admin_password.txt','w').write(secrets.token_urlsafe(24))"
	@echo "secrets/grafana_admin_password.txt ready"

build: secrets ## Build all images
	$(COMPOSE) build

up: secrets ## Build and start the stack, then wait for readiness
	$(COMPOSE) up -d --build
	@echo "waiting for the control plane to become ready..."
	@for i in $$(seq 1 40); do \
	  if curl -sf $(URL)/readyz >/dev/null 2>&1; then echo "ready -> $(URL)"; exit 0; fi; \
	  sleep 3; \
	done; echo "timed out; check: make logs"; exit 1

observability: secrets ## Start the stack plus Prometheus and Grafana
	$(COMPOSE) --profile observability up -d --build
	@echo "control plane $(URL)   prometheus http://localhost:9090   grafana http://localhost:3000"

down: ## Stop the stack (volumes are preserved)
	$(COMPOSE) --profile observability down

clean: ## Stop and delete volumes - wipes all collected history
	$(COMPOSE) --profile observability down -v

restart: ## Restart the control plane only
	$(COMPOSE) restart control-plane

ps: ## Container status
	$(COMPOSE) ps

logs: ## Follow the control plane logs
	$(COMPOSE) logs -f control-plane

# --------------------------------------------------------------- verification
verify: ## Full end-to-end verification, including a live incident (~2 min)
	$(PY) scripts/verify_local.py --url $(URL)

verify-quick: ## Verification without the live incident test
	$(PY) scripts/verify_local.py --url $(URL) --quick

test: ## Run the unit test suite
	$(PY) -m pytest tests -v

lint: ## Lint and format-check (requires ruff)
	ruff check services tests scripts
	ruff format --check services tests scripts

# ---------------------------------------------------------------------- demo
demo: ## Guided incident walkthrough
	@bash scripts/demo.sh

incident: ## Inject an incident: make incident SCENARIO=memory_leak TARGET=svc-checkout-api
	@curl -s -X POST -H 'content-type: application/json' \
	  -d '{"scenario":"$(or $(SCENARIO),cpu_spike)","resource_id":"$(or $(TARGET),svc-checkout-api)","duration_seconds":$(or $(DURATION),180)}' \
	  $(URL)/api/v1/incidents | $(PY) -m json.tool

stop-incidents: ## Cancel every active incident
	@curl -s -X DELETE $(URL)/api/v1/incidents | $(PY) -m json.tool

# -------------------------------------------------------------------- inspect
metrics: ## Show the CloudOps Prometheus metrics
	@curl -s $(URL)/metrics | grep -E '^cloudops_' | head -40

cost: ## Cost summary
	@curl -s $(URL)/api/v1/cost | $(PY) -c "import sys,json;d=json.load(sys.stdin);\
print(f\"monthly \$${d['total_monthly']:,.2f}  waste \$${d['waste_monthly']:,.2f} ({d['waste_pct']}%)\");\
[print(f\"  {k:12} \$${v:,.2f}\") for k,v in d['by_provider'].items()]"

recommendations: ## Top recommendations by saving
	@curl -s $(URL)/api/v1/recommendations | $(PY) -c "import sys,json;d=json.load(sys.stdin);\
print(f\"{d['summary']['total']} findings, \$${d['summary']['monthly_saving']:,.2f}/mo identified\");\
[print(f\"  [{r['severity']:8}] \$${r['monthly_saving']:>9,.2f}/mo  {r['title']}\") for r in d['recommendations'][:12]]"

open: ## Open the dashboard in a browser
	@$(PY) -c "import webbrowser;webbrowser.open('$(URL)')"

# ----------------------------------------------------------------- kubernetes
k8s-validate: ## Render and client-validate the manifests (no cluster needed)
	kubectl kustomize . > /dev/null && echo "kustomize build OK"
	kubectl apply -k . --dry-run=client -o name

k8s-apply: ## Apply to the current kubectl context
	kubectl apply -k .
	kubectl -n cloudops rollout status statefulset/control-plane --timeout=180s

k8s-delete: ## Remove everything from the cluster
	kubectl delete -k . --ignore-not-found
