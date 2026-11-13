#!/bin/bash

# Dump env vars with quoted values so special characters are preserved, same pattern
# as affiliate-projects-777/gsheet_reports - cron doesn't inherit the container's own
# env, so main.py sources /etc/environment itself at the top of each run.
printenv | while IFS='=' read -r key value; do
    echo "$key='$value'"
done > /etc/environment

# Run once immediately on container start (otherwise the first data point would only
# exist an hour after deploy), then hand off to cron for the hourly schedule.
cd /app && /usr/local/bin/python main.py >> /app/logs/cron.log 2>&1

cron -f
