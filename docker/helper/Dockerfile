FROM alpine:3.22
RUN apk add --no-cache git rsync
COPY --chmod=755 helper.sh /opt/fixie-helper/helper.sh
ENTRYPOINT ["/opt/fixie-helper/helper.sh"]
