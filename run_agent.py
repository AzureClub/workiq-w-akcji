"""CLI that fetches calendar via Work IQ and sends it to the Foundry agent."""

import subprocess
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

ENDPOINT = "https://agent-intercity-resource.services.ai.azure.com/api/projects/agent-intercity"
AGENT_NAME = "workiqagent"
AGENT_VERSION = "2"


def fetch_calendar_from_workiq() -> str:
    """Call Work IQ CLI to get today's calendar."""
    print("📅 Pobieram kalendarz z Work IQ...\n")
    workiq_cmd = r"C:\Users\msokolowski\AppData\Roaming\npm\workiq.cmd"
    result = subprocess.run(
        [workiq_cmd, "ask", "-q", "Jakie mam dzisiaj spotkania w kalendarzu? Podaj liste z godzinami."],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Work IQ error: {result.stderr}")
    return result.stdout.strip()


def ask_foundry_agent(question: str, calendar_context: str) -> str:
    """Send question + calendar context to the Foundry agent."""
    project = AIProjectClient(
        endpoint=ENDPOINT, credential=DefaultAzureCredential()
    )
    client = project.get_openai_client()

    prompt = (
        f"Oto dane z mojego kalendarza pobrane z Work IQ:\n\n"
        f"{calendar_context}\n\n"
        f"Na podstawie tych danych odpowiedz na pytanie: {question}"
    )

    response = client.responses.create(
        input=[{"role": "user", "content": prompt}],
        extra_body={
            "agent_reference": {
                "name": AGENT_NAME,
                "version": AGENT_VERSION,
                "type": "agent_reference",
            }
        },
    )
    return response.output_text


def main():
    question = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Podsumuj mój dzisiejszy kalendarz."

    calendar_data = fetch_calendar_from_workiq()
    print(f"✅ Kalendarz pobrany.\n")
    print(f"🤖 Pytam agenta Foundry: {question}\n")

    answer = ask_foundry_agent(question, calendar_data)
    print(answer)


if __name__ == "__main__":
    main()
