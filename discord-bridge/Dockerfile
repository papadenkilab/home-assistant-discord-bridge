ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8

# discord.py (pulls aiohttp) is the only runtime dependency.
COPY rootfs/app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    || pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY rootfs /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
