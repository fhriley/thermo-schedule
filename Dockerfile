FROM python:3.10-alpine

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

VOLUME /config
ENV LOGLEVEL=INFO

CMD [ "python", "./main.py" ]
