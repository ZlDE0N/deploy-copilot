"""Per-project automation: readiness score, GitHub Actions blue/green workflow draft, runbook,
checklist, DORA-style metrics. Everything is generated TEXT for human review — nothing is executed."""
from datetime import datetime

from plans import DEFAULT_STRATEGY, STRATEGIES, policy_checks

HEADER = """# BORRADOR generado por Deploy Copilot — requiere revisión humana antes de usarse.
# - Sin secretos: autenticación por OIDC; valores en `vars.*` / `secrets.*` del environment.
# - Crear environments: `{env}-staging` (sin aprobación) y `{env}` (con revisores obligatorios).
# - Recomendado: fijar cada `uses:` a un commit SHA (cadena de suministro).
"""

LOGIN = """      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
"""

ON = """name: deploy-%(repo)s
on:
  workflow_dispatch:
    inputs:
      environment:
        description: Entorno destino
        type: choice
        options: [qa, prod]
        default: qa
permissions:
  id-token: write
  contents: read
concurrency:
  group: deploy-%(repo)s-${{ inputs.environment }}
  cancel-in-progress: false
"""


def _build_steps(rtype: str, repo: str) -> str:
    if rtype.endswith("dotnet"):
        return ("      - uses: actions/checkout@v4\n"
                "      - uses: actions/setup-dotnet@v4\n"
                "        with:\n          dotnet-version: ${{ vars.DOTNET_VERSION }}\n"
                "      - run: dotnet publish -c Release -o out\n"
                "      - uses: actions/upload-artifact@v4\n"
                "        with:\n          name: app\n          path: out\n")
    return ("      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-node@v4\n"
            "        with:\n          node-version: ${{ vars.NODE_VERSION }}\n"
            "          cache: npm\n"
            "      - run: npm ci --ignore-scripts\n"
            "      - run: npm run build --if-present\n"
            "      - uses: actions/upload-artifact@v4\n"
            "        with:\n          name: app\n          path: .\n")


def _slot_workflow(r) -> str:
    is_fn = r["type"].startswith("function")
    cli = "functionapp" if is_fn else "webapp"
    deploy = ("      - uses: azure/functions-action@v1\n        with:\n          app-name: ${{ vars.APP_NAME }}\n"
              "          slot-name: staging\n          package: app\n") if is_fn else \
             ("      - uses: azure/webapps-deploy@v3\n        with:\n          app-name: ${{ vars.APP_NAME }}\n"
              "          slot-name: staging\n          package: app\n")
    return (ON % {"repo": r["name"]} +
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n" + _build_steps(r["type"], r["name"]) +
            "  deploy-staging:\n    needs: build\n    runs-on: ubuntu-latest\n"
            "    environment: ${{ inputs.environment }}-staging\n    steps:\n"
            "      - uses: actions/download-artifact@v4\n        with:\n          name: app\n          path: app\n" + LOGIN +
            f"      - name: Asegurar slot staging\n        run: |\n"
            f"          az {cli} deployment slot create -g ${{{{ vars.RESOURCE_GROUP }}}} -n ${{{{ vars.APP_NAME }}}} \\\n"
            f"            --slot staging --configuration-source ${{{{ vars.APP_NAME }}}} || true\n" + deploy +
            "      - name: Warm-up y smoke test (5 min máx)\n        run: |\n"
            "          URL=\"https://${{ vars.APP_NAME }}-staging.azurewebsites.net${{ vars.HEALTH_PATH }}\"\n"
            "          for i in $(seq 1 30); do\n"
            "            code=$(curl -s -o /dev/null -w '%{http_code}' \"$URL\" || true)\n"
            "            [ \"$code\" = \"200\" ] && ok=$((ok+1)) || ok=0\n"
            "            [ \"${ok:-0}\" -ge 5 ] && exit 0; sleep 10\n"
            "          done; echo \"smoke test falló\"; exit 1\n"
            "  swap:\n    needs: deploy-staging\n    runs-on: ubuntu-latest\n"
            "    environment: ${{ inputs.environment }}   # revisores obligatorios = aprobación humana\n    steps:\n" + LOGIN +
            f"      - name: Swap staging -> production\n        run: |\n"
            f"          az {cli} deployment slot swap -g ${{{{ vars.RESOURCE_GROUP }}}} -n ${{{{ vars.APP_NAME }}}} \\\n"
            f"            --slot staging --target-slot production\n"
            "  verify:\n    needs: swap\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: Observar producción 10 min\n        run: |\n"
            "          URL=\"https://${{ vars.APP_NAME }}.azurewebsites.net${{ vars.HEALTH_PATH }}\"\n"
            "          for i in $(seq 1 60); do\n"
            "            code=$(curl -s -o /dev/null -w '%{http_code}' \"$URL\" || true)\n"
            "            [ \"$code\" = \"200\" ] || { echo \"fallo $code\"; exit 1; }; sleep 10\n"
            "          done\n"
            "  rollback:\n    needs: [swap, verify]\n"
            "    if: ${{ failure() && needs.swap.result == 'success' }}   # solo si el swap ya ocurrió\n"
            "    runs-on: ubuntu-latest\n"
            "    environment: ${{ inputs.environment }}\n    steps:\n" + LOGIN +
            f"      - name: Swap de vuelta\n        run: |\n"
            f"          az {cli} deployment slot swap -g ${{{{ vars.RESOURCE_GROUP }}}} -n ${{{{ vars.APP_NAME }}}} \\\n"
            f"            --slot staging --target-slot production\n")


def _container_workflow(r) -> str:
    return (ON % {"repo": r["name"]} +
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    environment: ${{ inputs.environment }}-staging\n"
            "    outputs:\n      tag: ${{ steps.meta.outputs.tag }}\n    steps:\n"
            "      - uses: actions/checkout@v4\n" + LOGIN +
            "      - id: meta\n        run: echo \"tag=${GITHUB_SHA::7}\" >> \"$GITHUB_OUTPUT\"\n"
            "      - run: az acr build -r ${{ vars.ACR_NAME }} -t ${{ vars.IMAGE_NAME }}:${{ steps.meta.outputs.tag }} .\n"
            "  canary:\n    needs: build\n    runs-on: ubuntu-latest\n    environment: ${{ inputs.environment }}\n    steps:\n" + LOGIN +
            "      - name: Nueva revisión sin tráfico\n        run: |\n"
            "          az containerapp revision set-mode -n ${{ vars.APP_NAME }} -g ${{ vars.RESOURCE_GROUP }} --mode multiple\n"
            "          az containerapp update -n ${{ vars.APP_NAME }} -g ${{ vars.RESOURCE_GROUP }} \\\n"
            "            --image ${{ vars.ACR_NAME }}.azurecr.io/${{ vars.IMAGE_NAME }}:${{ needs.build.outputs.tag }} \\\n"
            "            --revision-suffix ${{ needs.build.outputs.tag }}\n"
            "          az containerapp ingress traffic set -n ${{ vars.APP_NAME }} -g ${{ vars.RESOURCE_GROUP }} \\\n"
            "            --revision-weight latest=0\n"
            "      - name: Split 10% -> 50% -> 100% con verificación\n        run: |\n"
            "          for w in 10 50 100; do\n"
            "            az containerapp ingress traffic set -n ${{ vars.APP_NAME }} -g ${{ vars.RESOURCE_GROUP }} \\\n"
            "              --revision-weight latest=$w\n"
            "            sleep 120\n"
            "            code=$(curl -s -o /dev/null -w '%{http_code}' \"https://${{ vars.APP_FQDN }}${{ vars.HEALTH_PATH }}\" || true)\n"
            "            [ \"$code\" = \"200\" ] || { echo \"fallo en $w%\"; exit 1; }\n"
            "          done\n"
            "  rollback:\n    needs: canary\n    if: failure()\n    runs-on: ubuntu-latest\n"
            "    environment: ${{ inputs.environment }}\n    steps:\n" + LOGIN +
            "      - name: Tráfico a la revisión anterior\n        run: |\n"
            "          PREV=$(az containerapp revision list -n ${{ vars.APP_NAME }} -g ${{ vars.RESOURCE_GROUP }} \\\n"
            "            --query \"[?properties.active && properties.trafficWeight==`0`] | [0].name\" -o tsv)\n"
            "          az containerapp ingress traffic set -n ${{ vars.APP_NAME }} -g ${{ vars.RESOURCE_GROUP }} \\\n"
            "            --revision-weight \"$PREV=100\"\n")


def _terraform_workflow(r) -> str:
    return (ON % {"repo": r["name"]} +
            "jobs:\n  plan:\n    runs-on: ubuntu-latest\n    environment: ${{ inputs.environment }}-staging\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: hashicorp/setup-terraform@v3\n" + LOGIN +
            "      - run: terraform init -input=false\n"
            "      - run: terraform fmt -check && terraform validate\n"
            "      - run: terraform plan -input=false -out=tfplan\n"
            "      - uses: actions/upload-artifact@v4\n        with:\n          name: tfplan\n          path: tfplan\n"
            "  apply:\n    needs: plan\n    runs-on: ubuntu-latest\n"
            "    environment: ${{ inputs.environment }}   # revisores obligatorios: revisar el plan (destroy/replace = bloqueo)\n"
            "    steps:\n      - uses: actions/checkout@v4\n      - uses: hashicorp/setup-terraform@v3\n" + LOGIN +
            "      - uses: actions/download-artifact@v4\n        with:\n          name: tfplan\n"
            "      - run: terraform init -input=false\n"
            "      - run: terraform apply -input=false tfplan\n")


def _frontend_workflow(r) -> str:
    return (ON % {"repo": r["name"]} +
            "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n" + _build_steps("node", r["name"]) +
            "  preview:\n    needs: build\n    runs-on: ubuntu-latest\n    environment: ${{ inputs.environment }}-staging\n"
            "    steps:\n      - run: echo \"TODO - desplegar a preview (destino de hosting por definir)\"\n"
            "  promote:\n    needs: preview\n    runs-on: ubuntu-latest\n    environment: ${{ inputs.environment }}\n"
            "    steps:\n      - run: echo \"TODO - promover build verificado a produccion\"\n")


def build_workflow(r, env="prod") -> dict:
    t = r["type"]
    if t.startswith(("function", "appservices")):
        body, kind = _slot_workflow(r), "slot-swap"
    elif t == "container":
        body, kind = _container_workflow(r), "revision-traffic-split"
    elif t == "terraform":
        body, kind = _terraform_workflow(r), "plan-review-apply"
    elif t.startswith("frontend"):
        body, kind = _frontend_workflow(r), "preview-promote"
    else:
        return {"supported": False, "reason": f"Tipo '{t}': sin plantilla (mobile/librerías/desconocido requieren definición manual)."}
    return {"supported": True, "kind": kind, "path": f".github/workflows/deploy-{r['name'].lower()}.yml",
            "yaml": HEADER.format(env=env) + body}


def importer_hint(r) -> str:
    return (f"gh actions-importer dry-run azure-devops pipeline --output-dir ./out "
            f"--azure-devops-project {r['org_project'] or '<proyecto>'} --pipeline-id <id>")


def readiness(con, r) -> dict:
    wf = (r["workflow"] or "")
    has_readme = con.execute("SELECT 1 FROM chunks WHERE kind='readme' AND repo=? LIMIT 1", (r["name"],)).fetchone()
    bad = con.execute("SELECT COUNT(*) FROM deploys WHERE repo=? AND result!='ok'", (r["name"],)).fetchone()[0]
    items = [
        ("CI/CD definido", 25, bool(r["has_workflow"])),
        ("Tipo de proyecto detectado", 15, r["type"] != "unknown"),
        ("Pipeline ya en GitHub Actions", 20, bool(wf) and "azure-pipelines" not in wf),
        ("Rama base consistente con su copia Azure (_az)", 10, not r["az_diverged"]),
        ("README presente", 10, bool(has_readme)),
        ("Estrategia blue/green aplicable", 10, r["type"] in STRATEGIES and not r["type"].startswith("mobile")),
        ("Sin despliegues fallidos", 10, bad == 0),
    ]
    got = sum(p for _, p, ok in items if ok)
    return {"score": got, "items": [{"name": n, "points": p, "ok": bool(ok)} for n, p, ok in items],
            "level": "listo" if got >= 70 else "casi" if got >= 45 else "trabajo"}


def checklist(r, env) -> list[str]:
    strat = STRATEGIES.get(r["type"], DEFAULT_STRATEGY)[0]
    out = ["Rama base confirmada y cambios mergeados (PR revisado por una persona)",
           "Variables del environment definidas (APP_NAME, RESOURCE_GROUP, HEALTH_PATH…) — sin secretos en el repo",
           "Credenciales por OIDC; sin PAT en remotos ni scripts"]
    if strat == "slot-swap":
        out += ["Slot `staging` existe y su configuración coincide con producción (settings 'slot sticky' revisadas)",
                "Endpoint de health responde 200 en staging antes del swap"]
    if strat == "revision-traffic-split":
        out += ["Modo multi-revisión activo en la Container App", "Revisión anterior identificada para rollback"]
    if strat == "plan-review-apply":
        out += ["`terraform plan` revisado: sin destroy/replace inesperados", "Lock de estado verificado"]
    if r["has_lfs"]:
        out.append("Runner con Git LFS disponible")
    if r["az_diverged"]:
        out.append(f"Confirmar fuente de verdad: `{r['branch']}` difiere de su rama _az")
    if env == "prod":
        out += ["Aprobación humana registrada", "Ventana de despliegue y canal de comunicación definidos"]
    return out


def runbook(r, env) -> str:
    strat, steps, rollback = STRATEGIES.get(r["type"], DEFAULT_STRATEGY)
    lines = [f"# Runbook de despliegue — {r['name']} ({env})", "",
             f"Tipo: {r['type']} · Estrategia: {strat} · Rama base: {r['branch']}", "", "## Antes de empezar"]
    lines += [f"- [ ] {c}" for c in checklist(r, env)]
    lines += ["", "## Pasos"] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    lines += ["", "## Verificación", "- Tasa de errores 5xx y latencia p95 estables durante la ventana de observación",
              "- Smoke test funcional del flujo crítico", "", "## Rollback", rollback, "",
              "## Si algo falla", "- Ejecutar rollback primero, investigar después",
              "- Registrar el resultado (ok/rollback/failed) para que el riesgo y las métricas mejoren"]
    return "\n".join(lines)


def dora(con, repo: str) -> dict:
    rows = con.execute("SELECT result, ts FROM deploys WHERE repo=? AND env='prod' ORDER BY ts", (repo,)).fetchall()
    if not rows:
        return {"deploys": 0}
    fmt = "%Y-%m-%d %H:%M:%S"
    ts = [datetime.strptime(x["ts"], fmt) for x in rows]
    weeks = max((ts[-1] - ts[0]).days / 7, 1)
    fails = [i for i, x in enumerate(rows) if x["result"] != "ok"]
    mttr = []
    for i in fails:
        nxt = next((j for j in range(i + 1, len(rows)) if rows[j]["result"] == "ok"), None)
        if nxt is not None:
            mttr.append((ts[nxt] - ts[i]).total_seconds() / 3600)
    cfr = len(fails) / len(rows)
    freq = len(rows) / weeks
    band = "elite" if cfr <= 0.05 and freq >= 7 else "alto" if cfr <= 0.10 and freq >= 1 else "medio" if cfr <= 0.15 else "bajo"
    return {"deploys": len(rows), "per_week": round(freq, 2), "change_failure_rate": round(cfr, 3),
            "mttr_hours": round(sum(mttr) / len(mttr), 1) if mttr else None, "band": band,
            "note": "Lead time no disponible (requiere commits/PR). Bandas orientativas tipo DORA."}
