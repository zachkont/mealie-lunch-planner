FROM python:3.12-alpine
RUN apk add --no-cache tzdata && \
    ln -snf /usr/share/zoneinfo/Europe/Athens /etc/localtime && \
    echo "Europe/Athens" > /etc/timezone
ENV TZ=Europe/Athens
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY generate_weekly_lunch_plan.py .
CMD ["python3", "generate_weekly_lunch_plan.py"]
