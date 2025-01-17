import asyncio
from loguru import logger
from time import sleep
import random
import requests
from concurrent.futures import ThreadPoolExecutor
from packaging import version as pyver
from typing import List

from django.conf import settings
from scripts.models import Script

from tacticalrmm.celery import app
from agents.models import Agent, AgentOutage
from core.models import CoreSettings
from logs.models import PendingAction

logger.configure(**settings.LOG_CONFIG)


def _check_agent_service(pk: int) -> None:
    agent = Agent.objects.get(pk=pk)
    r = asyncio.run(agent.nats_cmd({"func": "ping"}, timeout=2))
    if r == "pong":
        logger.info(
            f"Detected crashed tacticalagent service on {agent.hostname}, attempting recovery"
        )
        data = {"func": "recover", "payload": {"mode": "tacagent"}}
        asyncio.run(agent.nats_cmd(data, wait=False))


def _check_in_full(pk: int) -> None:
    agent = Agent.objects.get(pk=pk)
    asyncio.run(agent.nats_cmd({"func": "checkinfull"}, wait=False))


@app.task
def check_in_task() -> None:
    q = Agent.objects.only("pk", "version")
    agents: List[int] = [
        i.pk for i in q if pyver.parse(i.version) >= pyver.parse("1.1.12")
    ]
    with ThreadPoolExecutor() as executor:
        executor.map(_check_in_full, agents)


@app.task
def monitor_agents_task() -> None:
    q = Agent.objects.all()
    agents: List[int] = [i.pk for i in q if i.has_nats and i.status != "online"]
    with ThreadPoolExecutor() as executor:
        executor.map(_check_agent_service, agents)


def agent_update(pk: int) -> str:
    agent = Agent.objects.get(pk=pk)
    # skip if we can't determine the arch
    if agent.arch is None:
        logger.warning(f"Unable to determine arch on {agent.hostname}. Skipping.")
        return "noarch"

    version = settings.LATEST_AGENT_VER
    url = agent.winagent_dl
    inno = agent.win_inno_exe

    if agent.has_nats:
        if pyver.parse(agent.version) <= pyver.parse("1.1.11"):
            if agent.pendingactions.filter(
                action_type="agentupdate", status="pending"
            ).exists():
                action = agent.pendingactions.filter(
                    action_type="agentupdate", status="pending"
                ).last()
                if pyver.parse(action.details["version"]) < pyver.parse(version):
                    action.delete()
                else:
                    return "pending"

            PendingAction.objects.create(
                agent=agent,
                action_type="agentupdate",
                details={
                    "url": url,
                    "version": version,
                    "inno": inno,
                },
            )
        else:
            nats_data = {
                "func": "agentupdate",
                "payload": {
                    "url": url,
                    "version": version,
                    "inno": inno,
                },
            }
            asyncio.run(agent.nats_cmd(nats_data, wait=False))

        return "created"

    return "not supported"


@app.task
def send_agent_update_task(pks: List[int], version: str) -> None:
    q = Agent.objects.filter(pk__in=pks)
    agents: List[int] = [
        i.pk for i in q if pyver.parse(i.version) < pyver.parse(version)
    ]

    for pk in agents:
        agent_update(pk)


@app.task
def auto_self_agent_update_task() -> None:
    core = CoreSettings.objects.first()
    if not core.agent_auto_update:
        logger.info("Agent auto update is disabled. Skipping.")
        return

    q = Agent.objects.only("pk", "version")
    pks: List[int] = [
        i.pk
        for i in q
        if pyver.parse(i.version) < pyver.parse(settings.LATEST_AGENT_VER)
    ]

    for pk in pks:
        agent_update(pk)


@app.task
def sync_sysinfo_task():
    agents = Agent.objects.all()
    online = [
        i
        for i in agents
        if pyver.parse(i.version) >= pyver.parse("1.1.3") and i.status == "online"
    ]
    for agent in online:
        asyncio.run(agent.nats_cmd({"func": "sync"}, wait=False))


@app.task
def sync_salt_modules_task(pk):
    agent = Agent.objects.get(pk=pk)
    r = agent.salt_api_cmd(timeout=35, func="saltutil.sync_modules")
    # successful sync if new/charnged files: {'return': [{'MINION-15': ['modules.get_eventlog', 'modules.win_agent', 'etc...']}]}
    # successful sync with no new/changed files: {'return': [{'MINION-15': []}]}
    if r == "timeout" or r == "error":
        return f"Unable to sync modules {agent.salt_id}"

    return f"Successfully synced salt modules on {agent.hostname}"


@app.task
def batch_sync_modules_task():
    # sync modules, split into chunks of 50 agents to not overload salt
    agents = Agent.objects.all()
    online = [i.salt_id for i in agents]
    chunks = (online[i : i + 50] for i in range(0, len(online), 50))
    for chunk in chunks:
        Agent.salt_batch_async(minions=chunk, func="saltutil.sync_modules")
        sleep(10)


@app.task
def uninstall_agent_task(salt_id, has_nats):
    attempts = 0
    error = False

    if not has_nats:
        while 1:
            try:

                r = requests.post(
                    f"http://{settings.SALT_HOST}:8123/run",
                    json=[
                        {
                            "client": "local",
                            "tgt": salt_id,
                            "fun": "win_agent.uninstall_agent",
                            "timeout": 8,
                            "username": settings.SALT_USERNAME,
                            "password": settings.SALT_PASSWORD,
                            "eauth": "pam",
                        }
                    ],
                    timeout=10,
                )
                ret = r.json()["return"][0][salt_id]
            except Exception:
                attempts += 1
            else:
                if ret != "ok":
                    attempts += 1
                else:
                    attempts = 0

            if attempts >= 10:
                error = True
                break
            elif attempts == 0:
                break

    if error:
        logger.error(f"{salt_id} uninstall failed")
    else:
        logger.info(f"{salt_id} was successfully uninstalled")

    try:
        r = requests.post(
            f"http://{settings.SALT_HOST}:8123/run",
            json=[
                {
                    "client": "wheel",
                    "fun": "key.delete",
                    "match": salt_id,
                    "username": settings.SALT_USERNAME,
                    "password": settings.SALT_PASSWORD,
                    "eauth": "pam",
                }
            ],
            timeout=30,
        )
    except Exception:
        logger.error(f"{salt_id} unable to remove salt-key")

    return "ok"


@app.task
def agent_outage_email_task(pk):
    sleep(random.randint(1, 15))
    outage = AgentOutage.objects.get(pk=pk)
    outage.send_outage_email()
    outage.outage_email_sent = True
    outage.save(update_fields=["outage_email_sent"])


@app.task
def agent_recovery_email_task(pk):
    sleep(random.randint(1, 15))
    outage = AgentOutage.objects.get(pk=pk)
    outage.send_recovery_email()
    outage.recovery_email_sent = True
    outage.save(update_fields=["recovery_email_sent"])


@app.task
def agent_outage_sms_task(pk):
    sleep(random.randint(1, 3))
    outage = AgentOutage.objects.get(pk=pk)
    outage.send_outage_sms()
    outage.outage_sms_sent = True
    outage.save(update_fields=["outage_sms_sent"])


@app.task
def agent_recovery_sms_task(pk):
    sleep(random.randint(1, 3))
    outage = AgentOutage.objects.get(pk=pk)
    outage.send_recovery_sms()
    outage.recovery_sms_sent = True
    outage.save(update_fields=["recovery_sms_sent"])


@app.task
def agent_outages_task():
    agents = Agent.objects.only(
        "pk", "last_seen", "overdue_time", "overdue_email_alert", "overdue_text_alert"
    )

    for agent in agents:
        if agent.overdue_email_alert or agent.overdue_text_alert:
            if agent.status == "overdue":
                outages = AgentOutage.objects.filter(agent=agent)
                if outages and outages.last().is_active:
                    continue

                outage = AgentOutage(agent=agent)
                outage.save()

                if agent.overdue_email_alert and not agent.maintenance_mode:
                    agent_outage_email_task.delay(pk=outage.pk)

                if agent.overdue_text_alert and not agent.maintenance_mode:
                    agent_outage_sms_task.delay(pk=outage.pk)


@app.task
def install_salt_task(pk: int) -> None:
    sleep(20)
    agent = Agent.objects.get(pk=pk)
    asyncio.run(agent.nats_cmd({"func": "installsalt"}, wait=False))


@app.task
def run_script_email_results_task(
    agentpk: int, scriptpk: int, nats_timeout: int, nats_data: dict, emails: List[str]
):
    agent = Agent.objects.get(pk=agentpk)
    script = Script.objects.get(pk=scriptpk)
    nats_data["func"] = "runscriptfull"
    r = asyncio.run(agent.nats_cmd(nats_data, timeout=nats_timeout))
    if r == "timeout":
        logger.error(f"{agent.hostname} timed out running script.")
        return

    CORE = CoreSettings.objects.first()
    subject = f"{agent.hostname} {script.name} Results"
    exec_time = "{:.4f}".format(r["execution_time"])
    body = (
        subject
        + f"\nReturn code: {r['retcode']}\nExecution time: {exec_time} seconds\nStdout: {r['stdout']}\nStderr: {r['stderr']}"
    )

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = CORE.smtp_from_email

    if emails:
        msg["To"] = ", ".join(emails)
    else:
        msg["To"] = ", ".join(CORE.email_alert_recipients)

    msg.set_content(body)

    try:
        with smtplib.SMTP(CORE.smtp_host, CORE.smtp_port, timeout=20) as server:
            if CORE.smtp_requires_auth:
                server.ehlo()
                server.starttls()
                server.login(CORE.smtp_host_user, CORE.smtp_host_password)
                server.send_message(msg)
                server.quit()
            else:
                server.send_message(msg)
                server.quit()
    except Exception as e:
        logger.error(e)
