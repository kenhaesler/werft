# Werft-owned (CD M4); same Rocky org base as the runner image, digest-pinned.
# Resolved via: docker buildx imagetools inspect rockylinux/rockylinux:10
FROM rockylinux/rockylinux@sha256:827d37bc128288ccf160ee318bb3cb92d591164cb217e92f8bc61e3982ae1834
RUN dnf install -y --setopt=install_weak_deps=False squid && dnf clean all \
 && mkdir -p /var/log/squid /run/squid && chown squid:squid /var/log/squid /run/squid
COPY squid.conf /etc/squid/squid.conf
# allow/ is bind-mounted; squid -k parse at build proves the static config loads
RUN mkdir -p /etc/squid/allow && for k in 0 1 2 3; do : > /etc/squid/allow/slot$k.txt; done \
 && : > /etc/squid/allow/manager.txt && chown -R squid:squid /etc/squid/allow \
 && squid -k parse
USER squid
EXPOSE 3128
CMD ["squid", "-N", "-d", "1"]
