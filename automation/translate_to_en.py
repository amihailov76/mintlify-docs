#!/usr/bin/env python3
"""
translate_to_en.py — перевод изменений в ru/ репозитория mintlify-docs на английский.

Запускается в репозитории mintlify-docs. Определяет файлы, изменённые в папке ru/
с момента указанного коммита, переводит их в EN и сохраняет в папку en/.

Использование:
    python translate_to_en.py [--model MODEL] [--since COMMIT] [--dry-run] [--no-pr]

Опции:
    --model MODEL     Название модели (переопределяет config.yml)
    --since COMMIT    SHA коммита, с которого читать diff (по умолчанию: HEAD~1)
    --dry-run         Вывести план действий без изменения файлов
    --no-pr           Не создавать PR (только commit + push)
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

try:
    import httpx
    from openai import OpenAI
except ImportError:
    print("Установите зависимости: pip install openai pyyaml httpx")
    sys.exit(1)


# ─── Загрузка конфигурации ────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.yml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Git-операции ─────────────────────────────────────────────────────────────

def git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} завершился с ошибкой:\n{result.stderr}")
    return (result.stdout or "").strip()


def get_changed_ru_files(repo_path: Path, since: str) -> list[str]:
    """Возвращает список ru/-файлов, изменённых с указанного коммита."""
    diff_output = git(["diff", "--name-only", since, "HEAD"], cwd=repo_path)
    if not diff_output:
        return []
    return [
        f for f in diff_output.splitlines()
        if f.startswith("ru/") and f.endswith(".mdx")
    ]


def get_file_diff(repo_path: Path, filepath: str, since: str) -> str:
    """Возвращает unified diff конкретного файла между since и HEAD."""
    try:
        return git(["diff", since, "HEAD", "--", filepath], cwd=repo_path)
    except RuntimeError:
        return ""


def create_sync_branch(repo_path: Path, branch_prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    branch = f"{branch_prefix}-en/{timestamp}"
    git(["checkout", "-b", branch], cwd=repo_path)
    return branch


def commit_and_push(repo_path: Path, message: str, author_name: str, author_email: str) -> str:
    git(["add", "-A"], cwd=repo_path)
    git(
        [
            "-c", f"user.name={author_name}",
            "-c", f"user.email={author_email}",
            "commit", "-m", message,
        ],
        cwd=repo_path,
    )
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    git(["push", "-u", "origin", branch], cwd=repo_path)
    return branch


def create_pr(repo_path: Path, branch: str, base: str, title: str, body: str):
    try:
        subprocess.run(
            ["gh", "pr", "create",
             "--title", title,
             "--body", body,
             "--base", base,
             "--head", branch],
            cwd=repo_path,
            check=True,
        )
        print("  ✓ PR создан")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("  ⚠ Не удалось создать PR автоматически. Создайте PR вручную.")


# ─── LLM-клиент ───────────────────────────────────────────────────────────────

class LLMClient:
    def __init__(self, cfg: dict, model: str | None = None):
        llm_cfg = cfg["llm"]
        self.model = model or llm_cfg["default_model"]
        self.temperature = llm_cfg.get("temperature", 0.2)
        self.max_tokens = llm_cfg.get("max_tokens", 4096)
        self.timeout = llm_cfg.get("timeout_seconds", 180)

        api_key = (
            os.environ.get("LLM_API_KEY")
            or llm_cfg.get("api_key")
            or "no-key"
        )

        # SSL и прокси: обходим системный прокси для внутреннего LLM API.
        ca_bundle = os.environ.get("LLM_CA_BUNDLE")
        ssl_verify: bool | str = ca_bundle if ca_bundle else (
            os.environ.get("LLM_SSL_VERIFY", "true").lower() != "false"
        )
        bypass_proxy = os.environ.get("LLM_NO_PROXY", "true").lower() != "false"
        transport = httpx.HTTPTransport(verify=ssl_verify) if bypass_proxy else None
        http_client = httpx.Client(verify=ssl_verify, transport=transport)

        self.client = OpenAI(
            base_url=llm_cfg["api_url"],
            api_key=api_key,
            timeout=self.timeout,
            http_client=http_client,
        )

    def complete(self, system: str, user: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content.strip()
        return self._strip_code_fence(content)

    @staticmethod
    def _strip_code_fence(content: str) -> str:
        """Убирает код-фенсы, которые LLM иногда добавляет вокруг MDX-контента."""
        content = content.strip()

        def remove_fence(text: str) -> str:
            text = text.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                return "\n".join(lines).strip()
            return text

        if content.startswith("```"):
            return remove_fence(content)

        if content.startswith("---"):
            fm_end = content.find("---", 3)
            if fm_end != -1:
                frontmatter = content[:fm_end + 3]
                body = content[fm_end + 3:].strip()
                body = remove_fence(body)
                return frontmatter + "\n\n" + body

        return content


# ─── Промпт ───────────────────────────────────────────────────────────────────

SYSTEM_TRANSLATE_EN = """You are a technical documentation translator for Doc Reviewer.
Your task: update an English (EN) Mintlify MDX page to reflect changes made in the Russian (RU) source.

Rules:
- You will receive: (1) a git diff showing what changed in the RU file, (2) the full updated RU file, (3) the current EN file.
- Use the git diff to identify exactly what changed. Apply only those changes to the EN file — do not rewrite parts that haven't changed.
- Preserve all MDX components exactly: <Steps>, <Step>, <Card>, <CardGroup>, <Note>, <Warning>, <Tip>, <Accordion>, <AccordionGroup>, <Tabs>, <Tab>, <CodeGroup>, etc.
- Translate the frontmatter fields title and description to English if they changed.
- Change href links: replace /ru/ with /en/ in internal documentation links. Keep external URLs unchanged (github.com, ollama.com, etc.).
- Keep technical terms as-is: Doc Reviewer, LLM, API, SQLite, Playwright, PDF, DOCX, Markdown, MDX, XLS.
- Write clear, neutral technical English. No marketing language, no filler words.
- One topic per sentence. Use active voice. Prefer simple verb predicates over nominalizations.
- Return the complete updated EN MDX file — no explanations, no diff format, just the full file content.

If no git diff is provided, do a full translation of the RU file to English."""


# ─── Основная логика ──────────────────────────────────────────────────────────

def ru_path_to_en(ru_file: str) -> str:
    """Конвертирует путь ru/... в en/..."""
    return "en/" + ru_file[len("ru/"):]


def read_file(repo_path: Path, filepath: str) -> str:
    full_path = repo_path / filepath
    if full_path.exists():
        return full_path.read_text(encoding="utf-8")
    return ""


def write_file(repo_path: Path, filepath: str, content: str, dry_run: bool):
    full_path = repo_path / filepath
    full_path.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        print(f"  [dry-run] Записал бы: {full_path}")
    else:
        full_path.write_text(content, encoding="utf-8")
        print(f"  ✓ Обновлено: {full_path}")


# ─── Точка входа ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Перевод RU → EN в mintlify-docs")
    parser.add_argument("--model", help="Название модели LLM")
    parser.add_argument("--since", default="HEAD~1", help="Коммит, с которого читать diff")
    parser.add_argument("--dry-run", action="store_true", help="Не менять файлы")
    parser.add_argument("--no-pr", action="store_true", help="Не создавать PR")
    args = parser.parse_args()

    cfg = load_config()
    docs_path = Path(cfg["repos"]["mintlify_docs"]).resolve()

    print(f"📄 Репозиторий документации: {docs_path}")
    print(f"🔍 Анализирую изменения в ru/ с {args.since}...")

    # 1. Находим изменённые RU-файлы
    changed_ru = get_changed_ru_files(docs_path, args.since)
    if not changed_ru:
        print("✅ Нет изменений в ru/. Перевод не требуется.")
        return

    print(f"\n📝 Изменённые файлы ({len(changed_ru)}):")
    for f in changed_ru:
        print(f"  - {f}  →  {ru_path_to_en(f)}")

    if args.dry_run:
        print("\n[dry-run] Остановка перед изменением файлов.")
        return

    # 2. Инициализируем LLM-клиент
    llm = LLMClient(cfg, args.model)
    print(f"\n🤖 Используется модель: {llm.model}")

    # 3. Создаём ветку
    git_cfg = cfg["git"]
    branch = create_sync_branch(docs_path, git_cfg["branch_prefix"])
    print(f"\n🌿 Создана ветка: {branch}")

    translated = []

    for ru_file in changed_ru:
        en_file = ru_path_to_en(ru_file)
        print(f"\n⚙️  Перевожу: {ru_file} → {en_file}")

        ru_content = read_file(docs_path, ru_file)
        if not ru_content:
            print(f"  ⚠ Файл пустой или не найден, пропускаю.")
            continue

        current_en = read_file(docs_path, en_file)

        if current_en:
            ru_diff = get_file_diff(docs_path, ru_file, args.since)
            if ru_diff:
                print(f"  diff получен ({len(ru_diff.splitlines())} строк)")
                diff_section = f"""Changes in the RU file (git diff):
```diff
{ru_diff}
```

"""
            else:
                print(f"  ⚠ diff пустой, передаю полный файл")
                diff_section = ""

            user_prompt = f"""{diff_section}Full Russian (RU) version (after the changes):
```mdx
{ru_content}
```

Current English (EN) version:
```mdx
{current_en}
```

Apply the changes from the RU diff to the EN version. Return the complete updated EN document."""
        else:
            user_prompt = f"""Russian (RU) version of the page — translate to English:
```mdx
{ru_content}
```"""

        updated_en = llm.complete(SYSTEM_TRANSLATE_EN, user_prompt)
        write_file(docs_path, en_file, updated_en, dry_run=False)
        translated.append((ru_file, en_file))

    if not translated:
        print("\n⚠ Нет файлов для перевода.")
        return

    # 4. Коммит и пуш
    print(f"\n📤 Коммит и пуш...")
    files_list = ", ".join(en for _, en in translated)
    commit_msg = (
        f"docs: translate {len(translated)} page(s) RU→EN\n\n"
        f"Updated: {files_list}\n"
        f"Model: {llm.model}"
    )
    pushed_branch = commit_and_push(
        docs_path,
        commit_msg,
        git_cfg["commit_author_name"],
        git_cfg["commit_author_email"],
    )
    print(f"  ✓ Запушено в {pushed_branch}")

    # 5. PR
    if git_cfg.get("create_pr") and not args.no_pr:
        print(f"\n🔀 Создаю PR...")
        pr_body = (
            "## Автоматический перевод RU → EN\n\n"
            "Переведены страницы:\n"
            + "\n".join(f"- `{en}` ← `{ru}`" for ru, en in translated)
            + f"\n\n**Модель:** {llm.model}"
            + "\n\n*Создано автоматически скриптом translate_to_en.py*"
        )
        create_pr(
            docs_path,
            pushed_branch,
            git_cfg["pr_base_branch"],
            f"docs: translate {len(translated)} page(s) RU→EN",
            pr_body,
        )

    print(f"\n✅ Готово. Переведено страниц: {len(translated)}")


if __name__ == "__main__":
    main()
