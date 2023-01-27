#Dockerfile for ethoscope_metadata_server
FROM python:3.8
LABEL maintainer="Giorgio Gilestro <giorgio@gilest.ro>"

# pre-requisites
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-dejavu nano git gfortran file gcc cmake libcurl4-openssl-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

#install ethoscopy
#https://github.com/gilestrolab/ethoscopy
#RUN pip3 install ethoscopy
RUN pip install bottle pandas

WORKDIR /opt
RUN git clone https://github.com/gilestrolab/ethoscope_metadata_server.git

WORKDIR /opt/ethoscope_metadata_server
CMD ["python", "/opt/ethoscope_metadata_server/metadata_db_server.py", "--refresh", "--metadata", "/opt/ethoscope_metadata"]


# sudo docker run -d -p 8081:8081 \
#       --name ethoscope-metadata-server \
#       --volume /mnt/data/results:/mnt/ethoscope_results:ro \
#       --volume /mnt/data/ethoscope_metadata:/opt/ethoscope_metadata \
#       --restart=unless-stopped \
#       -e VIRTUAL_HOST="metadata.lab.gilest.ro" \
#       -e VIRTUAL_PORT="8081" \
#       -e LETSENCRYPT_HOST="metadata.lab.gilest.ro" \
#       -e LETSENCRYPT_EMAIL="giorgio@gilest.ro" \
#       ethoscope_metadata_server-lab.gilest.ro


# Update git repo without regenerating the Docker:
# sudo docker exec ethoscope-metadata-server bash -c 'cd /opt/ethoscope_metadata_server && git pull' 
# sudo docker restart ethoscope-metadata-server
