FROM continuumio/miniconda3:latest

WORKDIR /app

RUN conda create -n tao python=3.11 -y && \
    conda clean -afy

COPY requirements.txt .
RUN /opt/conda/envs/tao/bin/pip install --no-cache-dir -r requirements.txt

COPY . .

ENV BITTENSOR_NETWORK=finney
ENV API_HOST=0.0.0.0
ENV API_PORT=8000

EXPOSE 8000

CMD ["/opt/conda/envs/tao/bin/uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
