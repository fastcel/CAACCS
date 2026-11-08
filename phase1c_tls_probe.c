/*
 * Phase 1C TLS 1.3 timing probe (OpenSSL >= 3.5).
 *
 * Measures TCP connect() and SSL_connect() separately, using CLOCK_MONOTONIC.
 * OpenSSL library initialization, context creation, group configuration,
 * certificate loading, socket creation and SSL object setup are OUTSIDE
 * both timed regions. SSL_connect() still includes network waits, TLS
 * cryptographic operations, certificate verification and scheduler delays.
 * It is NOT a cryptographic-operation-only or on-wire packet timing metric.
 *
 * Usage:
 *   ./phase1c_tls_probe X25519 "$HOME/fyp-tls-test/server.crt"
 *   ./phase1c_tls_probe MLKEM768 "$HOME/fyp-tls-test/server.crt"
 *   ./phase1c_tls_probe X25519MLKEM768 "$HOME/fyp-tls-test/server.crt"
 * Optional testing arguments: [server IPv4] [port]
 */
#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static double elapsed_ms(struct timespec start, struct timespec end)
{
    return (end.tv_sec - start.tv_sec) * 1000.0
         + (end.tv_nsec - start.tv_nsec) / 1000000.0;
}

static int fail(const char *message)
{
    fprintf(stderr, "ERROR: %s\n", message);
    ERR_print_errors_fp(stderr);
    return 1;
}

int main(int argc, char **argv)
{
    if (argc < 3 || argc > 5) {
        fprintf(stderr,
            "Usage: %s <X25519|MLKEM768|X25519MLKEM768> <CA-certificate.pem> [IPv4] [port]\n",
            argv[0]);
        return 2;
    }

    const char *group = argv[1];
    const char *certfile = argv[2];
    const char *ip = argc >= 4 ? argv[3] : "10.10.0.2";
    const char *port_str = argc >= 5 ? argv[4] : "4433";
    if (strcmp(group, "X25519") && strcmp(group, "MLKEM768") &&
        strcmp(group, "X25519MLKEM768")) {
        fprintf(stderr, "ERROR: unsupported group %s\n", group);
        return 2;
    }

    char *port_end = NULL;
    errno = 0;
    long port = strtol(port_str, &port_end, 10);
    if (errno || !port_end || *port_end != '\0' || port < 1 || port > 65535) {
        fprintf(stderr, "ERROR: invalid TCP port: %s\n", port_str);
        return 2;
    }

    int result = 1;
    int fd = -1;
    SSL_CTX *ctx = NULL;
    SSL *ssl = NULL;
    X509 *peer = NULL;
    struct timespec tcp_start, tcp_end, tls_start, tls_end;
    struct sockaddr_in endpoint;
    memset(&endpoint, 0, sizeof(endpoint));
    endpoint.sin_family = AF_INET;
    endpoint.sin_port = htons((unsigned short)port);
    if (inet_pton(AF_INET, ip, &endpoint.sin_addr) != 1) {
        fprintf(stderr, "ERROR: invalid IPv4 address: %s\n", ip);
        return 2;
    }

    ctx = SSL_CTX_new(TLS_client_method());
    if (!ctx) { fail("SSL_CTX_new failed"); goto cleanup; }
    if (SSL_CTX_set_min_proto_version(ctx, TLS1_3_VERSION) != 1 ||
        SSL_CTX_set_max_proto_version(ctx, TLS1_3_VERSION) != 1 ||
        SSL_CTX_set_ciphersuites(ctx, "TLS_AES_128_GCM_SHA256") != 1 ||
        SSL_CTX_set1_groups_list(ctx, group) != 1) {
        fail("TLS 1.3, cipher suite or group configuration failed");
        goto cleanup;
    }
    SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, NULL);
    if (SSL_CTX_load_verify_locations(ctx, certfile, NULL) != 1) {
        fail("Cannot load CA/trusted server certificate");
        goto cleanup;
    }

    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) { perror("socket"); goto cleanup; }
    const struct timeval timeout = { .tv_sec = 10, .tv_usec = 0 };
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) < 0) {
        perror("setsockopt");
        goto cleanup;
    }

    if (clock_gettime(CLOCK_MONOTONIC, &tcp_start) != 0) {
        perror("clock_gettime"); goto cleanup;
    }
    if (connect(fd, (const struct sockaddr *)&endpoint, sizeof(endpoint)) != 0) {
        perror("connect"); goto cleanup;
    }
    if (clock_gettime(CLOCK_MONOTONIC, &tcp_end) != 0) {
        perror("clock_gettime"); goto cleanup;
    }

    ssl = SSL_new(ctx);
    if (!ssl) { fail("SSL_new failed"); goto cleanup; }
    if (SSL_set_fd(ssl, fd) != 1 ||
        X509_VERIFY_PARAM_set1_ip_asc(SSL_get0_param(ssl), ip) != 1) {
        fail("SSL socket setup or certificate IP verification setup failed");
        goto cleanup;
    }

    if (clock_gettime(CLOCK_MONOTONIC, &tls_start) != 0) {
        perror("clock_gettime"); goto cleanup;
    }
    int connect_result = SSL_connect(ssl);
    if (clock_gettime(CLOCK_MONOTONIC, &tls_end) != 0) {
        perror("clock_gettime"); goto cleanup;
    }
    if (connect_result != 1) {
        fprintf(stderr, "ERROR: SSL_connect failed (ssl_error=%d)\n",
                SSL_get_error(ssl, connect_result));
        ERR_print_errors_fp(stderr);
        goto cleanup;
    }

    peer = SSL_get1_peer_certificate(ssl);
    if (!peer || SSL_get_verify_result(ssl) != X509_V_OK) {
        fail("Peer certificate was missing or failed verification");
        goto cleanup;
    }
    const char *version = SSL_get_version(ssl);
    const char *negotiated_group = SSL_get0_group_name(ssl);
    const char *cipher = SSL_get_cipher(ssl);
    if (strcmp(version, "TLSv1.3") != 0 || !negotiated_group ||
        strcasecmp(negotiated_group, group) != 0 ||
        !cipher || strcmp(cipher, "TLS_AES_128_GCM_SHA256") != 0) {
        fprintf(stderr,
            "ERROR: unexpected negotiation: version=%s group=%s cipher=%s\n",
            version, negotiated_group ? negotiated_group : "(none)",
            cipher ? cipher : "(none)");
        goto cleanup;
    }

    printf("profile=%s tcp_ms=%.3f tls_handshake_ms=%.3f "
           "negotiated_group=%s openssl_group_name=%s certificate_verified=true "
           "version=%s cipher=%s session_reused=%d\n",
           group, elapsed_ms(tcp_start, tcp_end),
           elapsed_ms(tls_start, tls_end), group, negotiated_group, version, cipher,
           SSL_session_reused(ssl) ? 1 : 0);
    result = 0;

cleanup:
    X509_free(peer);
    SSL_free(ssl);  /* SSL_set_fd() created BIOs which also close fd. */
    if (!ssl && fd >= 0) close(fd);
    SSL_CTX_free(ctx);
    return result;
}
