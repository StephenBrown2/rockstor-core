"""
Copyright (c) 2012-2020 Rockstor, Inc. <http://rockstor.com>
This file is part of Rockstor.

Rockstor is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published
by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

Rockstor is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

import json
import logging
import os
import re
import shutil
import sys
from tempfile import mkstemp

from django.conf import settings

from system import services
from system.osi import run_command, md5sum, replace_line_if_found

logger = logging.getLogger(__name__)

SYSCTL = "/usr/bin/systemctl"
BASE_DIR = settings.ROOT_DIR
BASE_BIN = "{}bin".format(BASE_DIR)
DJANGO = "{}/django".format(BASE_BIN)
STAMP = "{}/.initrock".format(BASE_DIR)
FLASH_OPTIMIZE = "{}/flash-optimize".format(BASE_BIN)
PREP_DB = "{}/prep_db".format(BASE_BIN)
SUPERCTL = "{}/supervisorctl".format(BASE_BIN)
OPENSSL = "/usr/bin/openssl"
RPM = "/usr/bin/rpm"
YUM = "/usr/bin/yum"
IP = "/usr/sbin/ip"


def inet_addrs(interface=None):
    cmd = [IP, "addr", "show"]
    if interface is not None:
        cmd.append(interface)
    o, _, _ = run_command(cmd)
    ipaddr_list = []
    for l in o:
        if re.match("inet ", l.strip()) is not None:
            inet_fields = l.split()
            if len(inet_fields) > 1:
                ip_fields = inet_fields[1].split("/")
                if len(ip_fields) == 2:
                    ipaddr_list.append(ip_fields[0])
    return ipaddr_list


def current_rockstor_mgmt_ip(logger):
    # importing here because, APIWrapper needs postgres to be setup, so
    # importing at the top results in failure the first time.
    from smart_manager.models import Service

    ipaddr = None
    port = 443
    so = Service.objects.get(name="rockstor")

    if so.config is not None:
        config = json.loads(so.config)
        port = config["listener_port"]
        try:
            ipaddr_list = inet_addrs(config["network_interface"])
            if len(ipaddr_list) > 0:
                ipaddr = ipaddr_list[0]
        except Exception as e:
            # interface vanished.
            logger.exception(
                "Exception while gathering current management ip: {e}".format(e=e)
            )

    return ipaddr, port


def init_update_issue(logger):
    ipaddr, port = current_rockstor_mgmt_ip(logger)

    if ipaddr is None:
        ipaddr_list = inet_addrs()

    # We open w+ in case /etc/issue does not exist
    with open("/etc/issue", "w+") as ifo:
        if ipaddr is None and len(ipaddr_list) == 0:
            ifo.write("The system does not yet have an ip address.\n")
            ifo.write(
                "Rockstor cannot be configured using the web interface "
                "without this.\n\n"
            )
            ifo.write("Press Enter to receive updated network status\n")
            ifo.write(
                "If this message persists please login as root and "
                "configure your network using nmtui, then reboot.\n"
            )
        else:
            ifo.write("\nRockstor is successfully installed.\n\n")
            if ipaddr is not None:
                port_str = ""
                if port != 443:
                    port_str = ":{0}".format(port)
                ifo.write(
                    "web-ui is accessible with this link: "
                    "https://{0}{1}\n\n".format(ipaddr, port_str)
                )
            else:
                ifo.write("web-ui is accessible with the following links:\n")
                for i in ipaddr_list:
                    ifo.write("https://{0}\n".format(i))
    return ipaddr


def update_nginx(logger):
    try:
        ip, port = current_rockstor_mgmt_ip(logger)
        services.update_nginx(ip, port)
    except Exception as e:
        logger.exception("Exception while updating nginx: {e}".format(e=e))


def update_tz(logging):
    # update timezone variable in settings.py
    zonestr = os.path.realpath("/etc/localtime").split("zoneinfo/")[1]
    logging.info("system timezone = {}".format(zonestr))
    sfile = "{}/src/rockstor/settings.py".format(BASE_DIR)
    fo, npath = mkstemp()
    updated = False
    with open(sfile) as sfo, open(npath, "w") as tfo:
        for line in sfo.readlines():
            if re.match("TIME_ZONE = ", line) is not None:
                curzone = line.strip().split("= ")[1].strip("'")
                if curzone == zonestr:
                    break
                else:
                    tfo.write("TIME_ZONE = '{}'\n".format(zonestr))
                    updated = True
                    logging.info(
                        "Changed timezone from {} to {}".format(curzone, zonestr)
                    )
            else:
                tfo.write(line)
    if updated:
        shutil.move(npath, sfile)
    else:
        os.remove(npath)
    return updated


def bootstrap_sshd_config(logging):
    """
    Setup sshd_config options for Rockstor:
    1. Switch from the default /usr/lib/ssh/sftp-server subsystem
        to the internal-sftp subsystem required for sftp access to work.
        Note that this turns the SFTP service ON by default.
    2. Add our customization header and allow only the root user to connect.
    :param logging:
    :return:
    """
    sshd_config = "/etc/ssh/sshd_config"

    # Comment out default sftp subsystem
    fh, npath = mkstemp()
    sshdconf_source = "Subsystem\tsftp\t/usr/lib/ssh/sftp-server"
    sshdconf_target = "#{}".format(sshdconf_source)
    replaced = replace_line_if_found(
        sshd_config, npath, sshdconf_source, sshdconf_target
    )
    if replaced:
        shutil.move(npath, sshd_config)
        logging.info("updated sshd_config: commented out default Subsystem")
    else:
        os.remove(npath)

    # Set AllowUsers and Subsystem if needed
    with open(sshd_config, "a+") as sfo:
        logging.info("SSHD_CONFIG Customization")
        found = False
        for line in sfo.readlines():
            if (
                re.match(settings.SSHD_HEADER, line) is not None
                or re.match("AllowUsers ", line) is not None
                or re.match(settings.SFTP_STR, line) is not None
            ):
                # if header is found,
                found = True
                logging.info(
                    "sshd_config already has the updates. Leaving it unchanged."
                )
                break
        if not found:
            sfo.write("{}\n".format(settings.SSHD_HEADER))
            sfo.write("{}\n".format(settings.SFTP_STR))
            sfo.write("AllowUsers root\n")
            logging.info("updated sshd_config.")
            run_command([SYSCTL, "restart", "sshd"])


def require_postgres(logging):
    rs_dest = "/etc/systemd/system/rockstor-pre.service"
    rs_src = "{}/conf/rockstor-pre.service".format(BASE_DIR)
    logging.info("updating rockstor-pre service..")
    with open(rs_dest, "w") as dfo, open(rs_src) as sfo:
        for l in sfo.readlines():
            dfo.write(l)
            if re.match("After=postgresql.service", l) is not None:
                dfo.write("Requires=postgresql.service\n")
                logging.info("rockstor-pre now requires postgresql")
    run_command([SYSCTL, "daemon-reload"])
    return logging.info("systemd daemon reloaded")


def establish_shellinaboxd_service(logging):
    """
    Normalise on shellinaboxd as service name for shellinabox package.
    The https://download.opensuse.org/repositories/shells shellinabox package
    ( https://build.opensuse.org/package/show/shells/shellinabox ) uses a
    systemd service name of shellinabox.
    If we find no shellinaboxd service file and there exists a shellinabox one
    create a copy to enable us to normalise on shellinaboxd and avoid carrying
    another package just to implement this service name change as we are
    heavily invested in the shellinaboxd service name.
    :param logging: handle to logger.
    :return: logger handle.
    """
    logging.info("Normalising on shellinaboxd service file")
    required_sysd_name = "/usr/lib/systemd/system/shellinaboxd.service"
    opensuse_sysd_name = "/usr/lib/systemd/system/shellinabox.service"
    if os.path.exists(required_sysd_name):
        return logging.info("- shellinaboxd.service already exists")
    if os.path.exists(opensuse_sysd_name):
        shutil.copyfile(opensuse_sysd_name, required_sysd_name)
        run_command([SYSCTL, "daemon-reload"])
        return logging.info("- established shellinaboxd.service file")


def enable_rockstor_service(logging):
    rs_dest = "/etc/systemd/system/rockstor.service"
    rs_src = "{}/conf/rockstor.service".format(BASE_DIR)
    sum1 = md5sum(rs_dest)
    sum2 = md5sum(rs_src)
    if sum1 != sum2:
        logging.info("updating rockstor systemd service")
        shutil.copy(rs_src, rs_dest)
        run_command([SYSCTL, "enable", "rockstor"])
        logging.info("Done.")
    logging.info("rockstor service looks correct. Not updating.")


def enable_bootstrap_service(logging):
    name = "rockstor-bootstrap.service"
    bs_dest = "/etc/systemd/system/{}".format(name)
    bs_src = "{}/conf/{}".format(BASE_DIR, name)
    sum1 = "na"
    if os.path.isfile(bs_dest):
        sum1 = md5sum(bs_dest)
    sum2 = md5sum(bs_src)
    if sum1 != sum2:
        logging.info("updating rockstor-bootstrap systemd service")
        shutil.copy(bs_src, bs_dest)
        run_command([SYSCTL, "enable", name])
        run_command([SYSCTL, "daemon-reload"])
        return logging.info("Done.")
    return logging.info("{} looks correct. Not updating.".format(name))


def update_smb_service(logging):
    name = "smb.service"
    ss_dest = "/etc/systemd/system/{}".format(name)
    if not os.path.isfile(ss_dest):
        return logging.info("{} is not enabled. Not updating.".format(name))
    ss_src = "{}/conf/{}".format(BASE_DIR, name)
    sum1 = md5sum(ss_dest)
    sum2 = md5sum(ss_src)
    if sum1 != sum2:
        logging.info("Updating {}".format(name))
        shutil.copy(ss_src, ss_dest)
        run_command([SYSCTL, "daemon-reload"])
        return logging.info("Done.")
    return logging.info("{} looks correct. Not updating.".format(name))


def main():
    loglevel = logging.INFO
    if len(sys.argv) > 1 and sys.argv[1] == "-x":
        loglevel = logging.DEBUG
    logging.basicConfig(format="%(asctime)s: %(message)s", level=loglevel)

    cert_loc = "{}/certs/".format(BASE_DIR)
    if os.path.isdir(cert_loc):
        if not os.path.isfile(
            "{}/rockstor.cert".format(cert_loc)
        ) or not os.path.isfile("{}/rockstor.key".format(cert_loc)):
            shutil.rmtree(cert_loc)

    if not os.path.isdir(cert_loc):
        os.mkdir(cert_loc)
        dn = (
            "/C=US/ST=Rockstor user's state/L=Rockstor user's "
            "city/O=Rockstor user/OU=Rockstor dept/CN=rockstor.user"
        )
        logging.info("Creating openssl cert...")
        run_command(
            [
                OPENSSL,
                "req",
                "-nodes",
                "-newkey",
                "rsa:2048",
                "-keyout",
                "{}/first.key".format(cert_loc),
                "-out",
                "{}/rockstor.csr".format(cert_loc),
                "-subj",
                dn,
            ]
        )
        logging.debug("openssl cert created")
        logging.info("Creating rockstor key...")
        run_command(
            [
                OPENSSL,
                "rsa",
                "-in",
                "{}/first.key".format(cert_loc),
                "-out",
                "{}/rockstor.key".format(cert_loc),
            ]
        )
        logging.debug("rockstor key created")
        logging.info("Singing cert with rockstor key...")
        run_command(
            [
                OPENSSL,
                "x509",
                "-in",
                "{}/rockstor.csr".format(cert_loc),
                "-out",
                "{}/rockstor.cert".format(cert_loc),
                "-req",
                "-signkey",
                "{}/rockstor.key".format(cert_loc),
                "-days",
                "3650",
            ]
        )
        logging.debug("cert signed.")
        logging.info("restarting nginx...")
        run_command([SUPERCTL, "restart", "nginx"])

    logging.info("Checking for flash and Running flash optimizations if appropriate.")
    run_command([FLASH_OPTIMIZE, "-x"], throw=False)
    try:
        logging.info("Updating the timezone from the system")
        update_tz(logging)
    except Exception as e:
        logging.error("Exception while updating timezone: {}".format(e.__str__()))
        logging.exception(e)

    try:
        logging.info("Updating sshd_config")
        bootstrap_sshd_config(logging)
    except Exception as e:
        logging.error("Exception while updating sshd_config: {}".format(e.__str__()))

    if not os.path.isfile(STAMP):
        logging.info("Please be patient. This script could take a few minutes")
        shutil.copyfile(
            "{}/conf/django-hack.py".format(BASE_DIR), "{}/django".format(BASE_BIN)
        )
        run_command([SYSCTL, "enable", "postgresql"])
        logging.debug("Progresql enabled")
        pg_data = "/var/lib/pgsql/data"
        if os.path.isdir(pg_data):
            logger.debug("Deleting /var/lib/pgsql/data")
            shutil.rmtree("/var/lib/pgsql/data")
        logging.info("initializing Postgresql...")
        # Conditionally run this only if found (CentOS/RedHat script)
        if os.path.isfile("/usr/bin/postgresql-setup"):
            logger.debug("running postgresql-setup initdb")
            # Legacy (CentOS) db init command
            run_command(["/usr/bin/postgresql-setup", "initdb"])
        else:
            ## In eg openSUSE run the generic initdb from postgresql##-server
            if os.path.isfile("/usr/bin/initdb"):
                logger.debug("running generic initdb on {}".format(pg_data))
                run_command(
                    [
                        "su",
                        "-",
                        "postgres",
                        "-c",
                        "/usr/bin/initdb -D {}".format(pg_data),
                    ]
                )
        logging.info("Done.")
        run_command([SYSCTL, "restart", "postgresql"])
        run_command([SYSCTL, "status", "postgresql"])
        logging.debug("Postgresql restarted")
        logging.info("Creating app databases...")
        run_command(["su", "-", "postgres", "-c", "/usr/bin/createdb smartdb"])
        logging.debug("smartdb created")
        run_command(["su", "-", "postgres", "-c", "/usr/bin/createdb storageadmin"])
        logging.debug("storageadmin created")
        logging.info("Done")
        logging.info("Initializing app databases...")
        run_command(
            [
                "su",
                "-",
                "postgres",
                "-c",
                "psql -c \"CREATE ROLE rocky WITH SUPERUSER LOGIN PASSWORD 'rocky'\"",
            ]
        )  # noqa E501
        logging.debug("rocky ROLE created")
        run_command(
            [
                "su",
                "-",
                "postgres",
                "-c",
                "psql storageadmin -f {}/conf/storageadmin.sql.in".format(BASE_DIR),
            ]
        )  # noqa E501
        logging.debug("storageadmin app database loaded")
        run_command(
            [
                "su",
                "-",
                "postgres",
                "-c",
                "psql smartdb -f {}/conf/smartdb.sql.in".format(BASE_DIR),
            ]
        )
        logging.debug("smartdb app database loaded")
        logging.info("Done")
        run_command(
            [
                "cp",
                "-f",
                "{}/conf/postgresql.conf".format(BASE_DIR),
                "/var/lib/pgsql/data/",
            ]
        )
        logging.debug("postgresql.conf copied")
        run_command(
            ["cp", "-f", "{}/conf/pg_hba.conf".format(BASE_DIR), "/var/lib/pgsql/data/"]
        )
        logging.debug("pg_hba.conf copied")
        run_command([SYSCTL, "restart", "postgresql"])
        logging.info("Postgresql restarted")
        run_command(["touch", STAMP])
        require_postgres(logging)
        logging.info("Done")

    logging.info("Running app database migrations...")
    migration_cmd = [DJANGO, "migrate", "--noinput"]
    fake_migration_cmd = migration_cmd + ["--fake"]
    fake_initial_migration_cmd = migration_cmd + ["--fake-initial"]
    smartdb_opts = ["--database=smart_manager", "smart_manager"]

    # Migrate Content types before individual apps
    logger.debug("migrate (--fake-initial) contenttypes")
    run_command(
        fake_initial_migration_cmd + ["--database=default", "contenttypes"], log=True
    )

    for app in ("storageadmin", "smart_manager"):
        db = "default"
        if app == "smart_manager":
            db = app
        o, e, rc = run_command(
            [DJANGO, "showmigrations", "--list", "--database={}".format(db), app],
        )
        initial_faked = False
        for l in o:
            if l.strip() == "[X] 0001_initial":
                initial_faked = True
                break
        if not initial_faked:
            db_arg = "--database={}".format(db)
            logger.debug(
                "migrate (--fake) db=({}) app=({}) 0001_initial".format(db, app)
            )
            run_command(fake_migration_cmd + [db_arg, app, "0001_initial"], log=True)

    run_command(migration_cmd + ["auth"], log=True)
    run_command(migration_cmd + ["storageadmin"], log=True)
    run_command(migration_cmd + smartdb_opts, log=True)

    # Avoid re-apply from our six days 0002_08_updates to oauth2_provider
    # by faking so we can catch-up on remaining migrations.
    # Only do this if not already done, however, as we would otherwise incorrectly reset
    # the list of migrations applied (https://github.com/rockstor/rockstor-core/issues/2376).
    oauth2_provider_faked = False
    # Get current list of migrations
    o, e, rc = run_command([DJANGO, "showmigrations", "--list", "oauth2_provider"])
    for l in o:
        if l.strip() == "[X] 0002_08_updates":
            logger.debug(
                "The 0002_08_updates migration seems already applied, so skip it"
            )
            oauth2_provider_faked = True
            break
    if not oauth2_provider_faked:
        logger.debug(
            "The 0002_08_updates migration is not already applied so fake apply it now"
        )
        run_command(
            fake_migration_cmd + ["oauth2_provider", "0002_08_updates"], log=True
        )

    # Run all migrations for oauth2_provider
    run_command(migration_cmd + ["oauth2_provider"], log=True)

    logging.info("Done")
    logging.info("Running prepdb...")
    run_command([PREP_DB])
    logging.info("Done")

    logging.info("stopping firewalld...")
    run_command([SYSCTL, "stop", "firewalld"])
    run_command([SYSCTL, "disable", "firewalld"])
    logging.info("firewalld stopped and disabled")
    update_nginx(logging)

    init_update_issue(logging)

    establish_shellinaboxd_service(logging)
    enable_rockstor_service(logging)
    enable_bootstrap_service(logging)


if __name__ == "__main__":
    main()
