from namecom import Name

from .helpers import Factory


class NameFactory(Factory):
    def create(self, *args):
        return Name(*args)


class NameComClient:
    name_factory = NameFactory()

    def __init__(self, domain, options):
        self.domain = domain.strip()
        options = options.get("namecom", {})

        if "username" not in options or "token" not in options:
            raise ValueError("username and token need to be configured for name.com client")

        self.username = options["username"]
        self.token = options["password"]

        self.client = self.name_factory.get(self.username, self.token)

    def create_cname_record(self, subdomain, prefix, points_to):
        subdomain = f"{subdomain}.{self.prefix}"
        resp = self.client.create_record(self.domain, subdomain, "cname", points_to)
        return resp["id"]

    def delete_cname_record(self, subdomain, prefix):
        subdomain = f"{subdomain}.{self.prefix}"
        for record in self.client.list_records(self.domain, subdomain):
            self.client.delete_record(record["fqdn"][:-1], record["id"])
