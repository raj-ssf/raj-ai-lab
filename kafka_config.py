"""
Kafka client configuration helper.

If KAFKA_SECURITY_PROTOCOL=SSL, configures mTLS using cert files at
/etc/kafka-certs/{ca.crt, user.crt, user.key}. Otherwise plain connection.

Returns kwargs to splat into KafkaProducer / KafkaConsumer constructors.
"""
import os


def kafka_kwargs() -> dict:
    """Returns connection kwargs for kafka-python clients."""
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")
    security_protocol = os.environ.get("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")

    kwargs = {
        "bootstrap_servers": bootstrap,
    }

    if security_protocol == "SSL":
        cert_dir = os.environ.get("KAFKA_CERT_DIR", "/etc/kafka-certs")
        # cluster-ca-cert is mounted separately at /etc/kafka-cluster-ca/ca.crt
        # because Strimzi has two CAs: cluster-ca (signs brokers) and clients-ca
        # (signs users). The KafkaUser secret only has clients-ca, but the client
        # needs cluster-ca to verify the broker's cert.
        cluster_ca_dir = os.environ.get("KAFKA_CLUSTER_CA_DIR", "/etc/kafka-cluster-ca")
        ca_path = os.path.join(cluster_ca_dir, "ca.crt")
        if not os.path.exists(ca_path):
            ca_path = os.path.join(cert_dir, "ca.crt")  # fallback
        kwargs.update({
            "security_protocol": "SSL",
            "ssl_cafile": ca_path,
            "ssl_certfile": os.path.join(cert_dir, "user.crt"),
            "ssl_keyfile": os.path.join(cert_dir, "user.key"),
            "ssl_check_hostname": False,
        })
        print(f"[kafka] mTLS enabled, ca={ca_path}, certs={cert_dir}, bootstrap={bootstrap}")
    else:
        print(f"[kafka] Plain connection, bootstrap={bootstrap}")

    return kwargs
