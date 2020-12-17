"""
handler for an zerossl rest api as ca

for now, it supports validation via CNAME records registered via coredns redis interface
"""

import os
import json
import base64
import uuid
import re
import requests
import time

from enum import Enum
from OpenSSL import crypto
from cryptography.x509 import load_pem_x509_certificate

from acme.helper import convert_byte_to_string, convert_string_to_byte, csr_cn_get, csr_san_get, load_config
from dnsclient import Client, ClientType, Domain, DnsConfigError


class ChallengeType(Enum):
    HTTP = "HTTPS_CSR_HASH"
    DNS = "CNAME_CSR_HASH"
    EMAIL = "EMAIL"


class CertificateStatus(Enum):
    draft = "draft"
    pending_validation = "pending_validation"
    issued = "issued"
    cancelled = "cancelled"
    expiring_soon = "expiring_soon"
    expired = "expired"


class Certificate:

    def __init__(self, zerossl):
        self.zerossl = zerossl
        self.base_url = f"{self.zerossl.BASE_URL}/certificates"

    def get(self, cert_id):
        url = f"{self.base_url}/{cert_id}"
        return self.zerossl.get(url)

    def create(self, domains, csr, validity_days=90):
        if isinstance(domains, (list, tuple)):
            domains = ",".join(domains)

        return self.zerossl.post(self.base_url, {
            "certificate_domains": domains,
            "certificate_validity_days": validity_days,
            "certificate_csr": csr,
        })

    def verify(self, cert_id, challenge_type, email=None):
        url = f"{self.base_url}/{cert_id}/challenges"

        if isinstance(challenge_type, ChallengeType):
            challenge_type = challenge_type.value

        data = {
            "validation_method": challenge_type
        }

        if challenge_type == ChallengeType.EMAIL:
            if not email:
                raise ValueError(f"email is required for this challenge type: {challenge_type}")

            data["validation_email"] = email

        return self.zerossl.post(url, data)

    def download_inline(self, cert_id):
        url = f"{self.base_url}/{cert_id}/download/return"
        return self.zerossl.get(url)

    def cancel(self, cert_id):
        raise NotImplementedError


class ZeroSSLError(Exception):
    # TODO: should map to https://zerossl.com/documentation/api/error-codes/
    def __init__(self, code, message):
        super().__init__(message)

        self.code = code


class ZeroSSL:
    BASE_URL = "https://api.zerossl.com"

    def __init__(self, access_key):
        self.access_key = access_key
        self.certificate = Certificate(self)

    def request(self, url, method, data=None, json=None):
        resp = requests.request(
            url=url,
            method=method,
            params={"access_key": self.access_key},
            data=data,
            json=json
        )

        # FIXME: zerossl rest api return 200 too with an error object
        # need to handle this and raise ZeroSSLError in such case
        resp.raise_for_status()
        return resp.json()

    def get(self, url):
        return self.request(url, method="get")

    def post(self, url, data):
        return self.request(url, method="post", data=data)


class ConfigError(Exception):
    pass


def get_domain_config(config):
    """
    get domains from config

    Args:
        configparser (ConfigParser): config parser

    Returns:
        list of Domain
    """
    if "domains" not in config:
        raise ConfigError("domains config is missing")

    domains = []
    for name, prefixes in config["domains"].items():
        if name in config.defaults():
            continue
        allowed_prefixes = [prefix.lower().strip() for prefix in prefixes.split(",")]
        domains.append(Domain(name.lower().strip(), allowed_prefixes))
    return domains


def get_dns_options(config):
    """
    get dns options for suppored dns clients (e.g. coredns or namecom)

    Args:
        config (ConfigParser): config parser

    Returns:
        dict: dns options
    """
    options = {}

    for client_type in ClientType:
        name = client_type.value
        if name in config.sections():
            options[name] = config[name]

    return options

class CAhandler(object):
    """ZeroSSL CA handler"""

    def __init__(self, debug=None, logger=None):
        self.debug = debug
        self.logger = logger

        config = load_config(self.logger)['CAhandler']
        self.certificate_validity_days = config.get("cert_validity_days")
        self.access_key = config.get("access_key")
        self.domains = get_domain_config(config)
        self.dns_options = get_dns_options(config)
        self.zerossl = ZeroSSL(self.access_key)

        if ClientType.NAMECOM.value in self.dns_options:
            client_type = ClientType.NAMECOM
        elif ClientType.COREDNS.value in self.dns_options:
            client_type = ClientType.COREDNS
        else:
            raise DnsConfigError("no dns client is configured (e.g namecom or coredns)")

        self.dns_client = Client(client_type, self.domains, self.dns_options)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_domain_names(self, csr):
        names = []

        cn = csr_cn_get(self.logger, csr)
        if cn:
            names.append(cn)

        for item in csr_san_get(self.logger, csr):
            names.append(item.split(":")[-1])

        return names

    def check_domain(self, domain):
        pass

    def create_dns_records(self, domain, points_to):
        self.check_domain(domain)
        subdomain = domain.replace(f".{self.coredns_domain}", "").lower()
        self.coredns.register_cname_record(subdomain, points_to)

    def try_verify_domain(self, cert_id, trials=4):
        details = {}

        while trials:
            result = self.zerossl.certificate.verify(cert_id, ChallengeType.DNS.value)
            if not result.get("success", True):
                # success is set to False, set error details value
                if "details" in result:
                    details = result["details"]
                else:
                    details = result["error"]

                trials -= 1
            else:
                # return result, the cert object (as json)
                return result

        raise RuntimeError(str(details))

    def poll_until_issued(self, cert_id, timeout=180, delay=0.2):
        time_start = time.time()

        while time.time() - time_start < timeout:
            cert_data = self.zerossl.certificate.get(cert_id)
            status = CertificateStatus(cert_data["status"])
            if status in [CertificateStatus.issued, CertificateStatus.expiring_soon]:
                return cert_data
            time.sleep(delay)

        raise TimeoutError(f"timeout ({timeout}s) while waiting for certificate to be issued")

    def enroll(self, csr):
        """ enroll certificate """
        self.logger.debug('CAhandler.enroll()')

        error = None
        cert_bundle = None
        cert_raw = None

        # get domains from csr
        domains = self.get_domain_names(csr)

        # create certificate (csr must be 2048-bit encrypted)
        try:
            cert_data = self.zerossl.certificate.create(domains, csr, self.certificate_validity_days)
        except requests.HTTPError as http_error:
            error = f"error while creating certificate {http_error}"

        if not error:
            if cert_data.get("success") is False:
                error = cert_data["error"]

        # now we have certificate id and dns challenge data
        if not error:
            cert_id = cert_data["id"]
            # TODO: more status logic need to be handled, e.g. renewal?
            status = CertificateStatus(cert_data["status"])
            if status in [CertificateStatus.draft, CertificateStatus.expired]:
                # try to validate
                all_validations = cert_data["validation"]["other_methods"]
                for domain, validations in all_validations.items():
                    # put dns records
                    try:
                        self.create_dns_records(validations["cname_validation_p1"], validations["cname_validation_p2"])
                    except Exception as exc:
                        error = f"error while registering dns records for {domain}: {exc}"

                if not error:
                    # try verify the challenge
                    try:
                        self.try_verify_domain(cert_id)
                    except Exception as exc:
                        error = f"could not verify the challenge for one of the domains: {exc}"

            if not error:
                # now poll on the certificated until status change
                try:
                    self.poll_until_issued(cert_id)
                except TimeoutError as timeout_error:
                    error = timeout_error

                if not error:
                    # download the cert and return it as following
                    result = self.zerossl.certificate.download_inline(cert_id)
                    # in PEM format
                    cert_bundle = result["ca_bundle.crt"]
                    cert_pem = result["certificate.crt"]
                    # tbh, don't know why to repeat, but chaining only cert_pem + bundle didn't work
                    # with certbot as a client, it fails with:
                    # "failed to parse fullchain into cert and chain: less than 2 certificates in chain"
                    cert_bundle = "\n".join([cert_pem, cert_bundle, cert_bundle])
                    # cert as OpenSSL.crypto.X509
                    cert = crypto.X509.from_cryptography(load_pem_x509_certificate(convert_string_to_byte(cert_pem)))
                    # convert to raw cert as needed by caller
                    cert_raw = convert_byte_to_string(base64.b64encode(crypto.dump_certificate(crypto.FILETYPE_ASN1, cert)))

        return(error, cert_bundle, cert_raw, None)


    def poll(self, _cert_name, poll_identifier, _csr):
        """ poll status of pending CSR and download certificates """
        self.logger.debug('CAhandler.poll()')

        error = 'Method not implemented.'
        cert_bundle = None
        cert_raw = None
        rejected = False

        self.logger.debug('CAhandler.poll() ended')
        return(error, cert_bundle, cert_raw, poll_identifier, rejected)

    def revoke(self, cert, rev_reason='unspecified', rev_date=None):
        """ revoke certificate """
        # for revocation, we need to have the zerossl certificate id (need to be stored in enrollment)
        # ...
        self.logger.debug('CAhandler.revoke()')

        error = 'Method not implemented.'
        cert_bundle = None
        cert_raw = None

        self.logger.debug('CAhandler.revoke() ended with error: {0}'.format(error))
        return (error, cert_bundle, cert_raw)

    def trigger(self, _payload):
        """ process trigger message and return certificate """
        self.logger.debug('CAhandler.trigger()')

        error = 'Method not implemented.'
        cert_bundle = None
        cert_raw = None

        self.logger.debug('CAhandler.trigger() ended with error: {0}'.format(error))
        return (error, cert_bundle, cert_raw)
