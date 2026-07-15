#!/usr/bin/env python3
"""
Busca stats do GitHub (uptime da conta, repos, commits, linhas de código)
e injeta os valores no card.svg, procurando pelos tspans com id="stat-*".

Precisa da env var GH_TOKEN (um Personal Access Token, veja o workflow).
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

GITHUB_USERNAME = os.environ.get("GH_USERNAME", "heytulio")
TOKEN = os.environ["GH_TOKEN"]
# aceita um ou mais arquivos, separados por vírgula: "dark_mode.svg,light_mode.svg"
SVG_PATHS = [p.strip() for p in os.environ.get("SVG_PATHS", "card.svg").split(",") if p.strip()]

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def gh_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def gh_graphql(query, variables=None):
    r = requests.post(
        GRAPHQL,
        headers=HEADERS,
        json={"query": query, "variables": variables or {}},
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


# ---------------------------------------------------------------------------
# 1. Uptime: tempo desde a criação da conta
# ---------------------------------------------------------------------------
def get_uptime():
    user = gh_get(f"{API}/users/{GITHUB_USERNAME}")
    created = datetime.strptime(user["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    now = datetime.now(timezone.utc)

    years = now.year - created.year
    months = now.month - created.month
    days = now.day - created.day

    if days < 0:
        months -= 1
        # dias no mês anterior ao mês atual
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month != 1 else now.year - 1
        from calendar import monthrange

        days += monthrange(prev_year, prev_month)[1]
    if months < 0:
        years -= 1
        months += 12

    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    parts.append(f"{months} month{'s' if months != 1 else ''}")
    parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# 2. Repos: total de repositórios próprios (públicos + privados que o token vê)
# ---------------------------------------------------------------------------
def get_repo_count():
    query = """
    query {
      viewer {
        repositories(ownerAffiliations: OWNER, isFork: false) {
          totalCount
        }
      }
    }
    """
    data = gh_graphql(query)
    return data["viewer"]["repositories"]["totalCount"]


# ---------------------------------------------------------------------------
# 3. Commits: soma de contribuições de commit, ano a ano, desde a criação da conta
#    (contributionsCollection só cobre no máximo 1 ano por consulta)
# ---------------------------------------------------------------------------
def get_total_commits():
    user = gh_get(f"{API}/users/{GITHUB_USERNAME}")
    created = datetime.strptime(user["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    now = datetime.now(timezone.utc)

    query = """
    query($from: DateTime!, $to: DateTime!) {
      viewer {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          restrictedContributionsCount
        }
      }
    }
    """

    total = 0
    year_start = created
    while year_start < now:
        year_end = min(
            year_start.replace(year=year_start.year + 1), now
        )
        data = gh_graphql(
            query,
            {
                "from": year_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": year_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        cc = data["viewer"]["contributionsCollection"]
        # restrictedContributionsCount cobre commits em repos privados
        # que o viewer não pode listar individualmente, mas ainda contam.
        total += cc["totalCommitContributions"] + cc["restrictedContributionsCount"]
        year_start = year_end

    return total


# ---------------------------------------------------------------------------
# 4. Lines of code: soma de additions/deletions do autor em cada repo
#    (usa o endpoint de contributor stats, sem precisar clonar nada)
# ---------------------------------------------------------------------------
def get_contributor_stats(owner, name, retries=6, delay=5):
    """
    O endpoint stats/contributors às vezes responde 202 com corpo vazio
    enquanto o GitHub ainda está calculando as estatísticas do repo
    (comum na primeira consulta). Também pode devolver 204/409 para repos
    vazios. Aqui a gente espera e tenta de novo, e desiste sem quebrar o
    resto do script se não conseguir depois de algumas tentativas.
    """
    url = f"{API}/repos/{owner}/{name}/stats/contributors"
    for attempt in range(retries):
        r = requests.get(url, headers=HEADERS)

        if r.status_code == 202:
            print(f"    {owner}/{name}: GitHub ainda calculando, aguardando...")
            time.sleep(delay)
            continue

        if r.status_code in (204, 404, 409):
            return []

        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            print(f"    {owner}/{name}: erro {r.status_code}, pulando ({e})", file=sys.stderr)
            return []

        if not r.text.strip():
            return []

        return r.json()

    print(f"    {owner}/{name}: sem resposta após {retries} tentativas, pulando", file=sys.stderr)
    return []


def get_lines_of_code():
    repos = []
    page = 1
    while True:
        batch = gh_get(
            f"{API}/user/repos",
            params={
                "affiliation": "owner",
                "per_page": 100,
                "page": page,
                "visibility": "all",
            },
        )
        if not batch:
            break
        repos.extend(batch)
        page += 1

    additions, deletions = 0, 0
    for repo in repos:
        if repo.get("fork"):
            continue
        owner, name = repo["owner"]["login"], repo["name"]
        stats = get_contributor_stats(owner, name)
        if not isinstance(stats, list):
            continue
        for contributor in stats:
            author = contributor.get("author") or {}
            if author.get("login", "").lower() != GITHUB_USERNAME.lower():
                continue
            for week in contributor.get("weeks", []):
                additions += week.get("a", 0)
                deletions += week.get("d", 0)

    return additions, deletions


def fmt(n):
    return f"{n:,}".replace(",", ".")


def inject(svg_text, stat_id, value):
    pattern = rf'(id="{stat_id}"[^>]*>)[^<]*(<)'
    replacement = rf"\g<1>{value}\g<2>"
    new_text, n = re.subn(pattern, replacement, svg_text)
    if n == 0:
        print(f"AVISO: id '{stat_id}' não encontrado no SVG", file=sys.stderr)
    return new_text


def main():
    print("Calculando uptime...")
    uptime = get_uptime()
    print(f"  -> {uptime}")

    print("Contando repositórios...")
    repos = get_repo_count()
    print(f"  -> {repos}")

    print("Somando commits (isso demora um pouco)...")
    commits = get_total_commits()
    print(f"  -> {commits}")

    print("Somando linhas de código (isso demora mais ainda)...")
    additions, deletions = get_lines_of_code()
    print(f"  -> +{additions} / -{deletions}")

    loc_str = f"{fmt(additions + deletions)} (+{fmt(additions)} / -{fmt(deletions)})"

    for svg_path in SVG_PATHS:
        with open(svg_path, "r", encoding="utf-8") as f:
            svg = f.read()

        svg = inject(svg, "stat-uptime", uptime)
        svg = inject(svg, "stat-repos", fmt(repos))
        svg = inject(svg, "stat-commits", fmt(commits))
        svg = inject(svg, "stat-loc", loc_str)

        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg)

        print(f"{svg_path} atualizado.")


if __name__ == "__main__":
    main()