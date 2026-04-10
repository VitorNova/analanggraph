module.exports = {
  apps: [
    {
      name: "ana-langgraph",
      cwd: "/var/www/ana-langgraph",
      interpreter: "/var/www/ana-langgraph/.venv/bin/python",
      script: "/var/www/ana-langgraph/.venv/bin/uvicorn",
      args: "api.app:app --host 0.0.0.0 --port 3202",
      env: {
        PYTHONPATH: "/var/www/ana-langgraph",
        PYTHONUNBUFFERED: "1",
      },
      max_restarts: 5,
      restart_delay: 3000,
    },
    // Job: Manutenção preventiva D-7 às 9h
    {
      name: "ana-manutencao-job",
      script: "/var/www/ana-langgraph/.venv/bin/python",
      args: "jobs/manutencao_job.py",
      cwd: "/var/www/ana-langgraph",
      interpreter: "none",
      cron_restart: "0 9 * * 1-5",
      autorestart: false,
      watch: false,
      env: {
        PYTHONPATH: "/var/www/ana-langgraph",
      },
    },
    // Job: Billing (disparos de cobrança) às 9h
    {
      name: "ana-billing-job",
      script: "/var/www/ana-langgraph/.venv/bin/python",
      args: "jobs/billing_job.py",
      cwd: "/var/www/ana-langgraph",
      interpreter: "none",
      cron_restart: "0 9 * * 1-5",
      autorestart: false,
      watch: false,
      env: {
        PYTHONPATH: "/var/www/ana-langgraph",
      },
    },
  ],
};
