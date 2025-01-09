"""
SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
SPDX-License-Identifier: AGPL-3.0-or-later
SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""
import asyncio, atexit, copy, importlib.metadata, json, logging, os, re, signal, shutil, sys, textwrap, time
import getopt  # pylint: disable=deprecated-module
import urllib.parse as urllib_parse
import urllib.request as urllib_request
from collections.abc import Iterable
from datetime import datetime
from gettext import gettext as _
from logging.handlers import RotatingFileHandler
from typing import Any, Final

import certifi, colorama, nodriver
from ruamel.yaml import YAML
from wcmatch import glob

from . import utils, resources, extract
from .i18n import Locale, get_current_locale, set_current_locale, get_translating_logger, pluralize
from .utils import abspath, ainput, apply_defaults, ensure, is_frozen, safe_get, parse_datetime
from .web_scraping_mixin import By, Element, Page, Is, WebScrapingMixin
from ._version import __version__

# W0406: possibly a bug, see https://github.com/PyCQA/pylint/issues/3933

LOG_ROOT:Final[logging.Logger] = logging.getLogger()
LOG:Final[logging.Logger] = get_translating_logger(__name__)
LOG.setLevel(logging.INFO)

colorama.just_fix_windows_console()


class KleinanzeigenBot(WebScrapingMixin):

    def __init__(self) -> None:

        # workaround for https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/295
        # see https://github.com/pyinstaller/pyinstaller/issues/7229#issuecomment-1309383026
        os.environ["SSL_CERT_FILE"] = certifi.where()

        super().__init__()

        self.root_url = "https://www.kleinanzeigen.de"

        self.config:dict[str, Any] = {}
        self.config_file_path = abspath("config.yaml")

        self.categories:dict[str, str] = {}

        self.file_log:logging.FileHandler | None = None
        log_file_basename = is_frozen() and os.path.splitext(os.path.basename(sys.executable))[0] or self.__module__
        self.log_file_path:str | None = abspath(f"{log_file_basename}.log")

        self.command = "help"
        self.ads_selector = "due"
        self.keep_old_ads = False

    def __del__(self) -> None:
        if self.file_log:
            LOG_ROOT.removeHandler(self.file_log)
        self.close_browser_session()

    def get_version(self) -> str:
        return __version__

    async def run(self, args:list[str]) -> None:
        self.parse_args(args)
        try:
            match self.command:
                case "help":
                    self.show_help()
                case "version":
                    print(self.get_version())
                case "verify":
                    self.configure_file_logging()
                    self.load_config()
                    self.load_ads()
                    LOG.info("############################################")
                    LOG.info("DONE: No configuration errors found.")
                    LOG.info("############################################")
                case "publish":
                    self.configure_file_logging()
                    self.load_config()

                    if not (self.ads_selector in {'all', 'new', 'due'} or re.compile(r'\d+[,\d+]*').search(self.ads_selector)):
                        LOG.warning('You provided no ads selector. Defaulting to "due".')
                        self.ads_selector = 'due'

                    if ads := self.load_ads():
                        await self.create_browser_session()
                        await self.login()
                        await self.publish_ads(ads)
                    else:
                        LOG.info("############################################")
                        LOG.info("DONE: No new/outdated ads found.")
                        LOG.info("############################################")
                case "delete":
                    self.configure_file_logging()
                    self.load_config()
                    if ads := self.load_ads():
                        await self.create_browser_session()
                        await self.login()
                        await self.delete_ads(ads)
                    else:
                        LOG.info("############################################")
                        LOG.info("DONE: No ads to delete found.")
                        LOG.info("############################################")
                case "download":
                    self.configure_file_logging()
                    # ad IDs depends on selector
                    if not (self.ads_selector in {'all', 'new'} or re.compile(r'\d+[,\d+]*').search(self.ads_selector)):
                        LOG.warning('You provided no ads selector. Defaulting to "new".')
                        self.ads_selector = 'new'
                    self.load_config()
                    await self.create_browser_session()
                    await self.login()
                    await self.download_ads()

                case _:
                    LOG.error("Unknown command: %s", self.command)
                    sys.exit(2)
        finally:
            self.close_browser_session()

    def show_help(self) -> None:
        if is_frozen():
            exe = sys.argv[0]
        elif os.getenv("PDM_PROJECT_ROOT", ""):
            exe = "pdm run app"
        else:
            exe = "python -m kleinanzeigen_bot"

        if get_current_locale().language == "de":
            print(textwrap.dedent(f"""\
            Verwendung: {colorama.Fore.LIGHTMAGENTA_EX}{exe} BEFEHL [OPTIONEN]{colorama.Style.RESET_ALL}

            Befehle:
              publish  - (Wieder-)Veröffentlicht Anzeigen
              verify   - Überprüft die Konfigurationsdateien
              delete   - Löscht Anzeigen
              download - Lädt eine oder mehrere Anzeigen herunter
              --
              help     - Zeigt diese Hilfe an (Standardbefehl)
              version  - Zeigt die Version der Anwendung an

            Optionen:
              --ads=all|due|new|<id(s)> (publish) - Gibt an, welche Anzeigen (erneut) veröffentlicht werden sollen (STANDARD: due)
                    Mögliche Werte:
                    * all: Veröffentlicht alle Anzeigen erneut, ignoriert republication_interval
                    * due: Veröffentlicht alle neuen Anzeigen und erneut entsprechend dem republication_interval
                    * new: Veröffentlicht nur neue Anzeigen (d.h. Anzeigen ohne ID in der Konfigurationsdatei)
                    * <id(s)>: Gibt eine oder mehrere Anzeigen-IDs an, die veröffentlicht werden sollen, z. B. "--ads=1,2,3", ignoriert republication_interval
              --ads=all|new|<id(s)> (download) - Gibt an, welche Anzeigen heruntergeladen werden sollen (STANDARD: new)
                    Mögliche Werte:
                    * all: Lädt alle Anzeigen aus Ihrem Profil herunter
                    * new: Lädt Anzeigen aus Ihrem Profil herunter, die lokal noch nicht gespeichert sind
                    * <id(s)>: Gibt eine oder mehrere Anzeigen-IDs zum Herunterladen an, z. B. "--ads=1,2,3"
              --force           - Alias für '--ads=all'
              --keep-old        - Verhindert das Löschen alter Anzeigen bei erneuter Veröffentlichung
              --config=<PATH>   - Pfad zur YAML- oder JSON-Konfigurationsdatei (STANDARD: ./config.yaml)
              --logfile=<PATH>  - Pfad zur Protokolldatei (STANDARD: ./kleinanzeigen-bot.log)
              --lang=en|de      - Anzeigesprache (STANDARD: Systemsprache, wenn unterstützt, sonst Englisch)
              -v, --verbose     - Aktiviert detaillierte Ausgabe – nur nützlich zur Fehlerbehebung
            """.rstrip()))
        else:
            print(textwrap.dedent(f"""\
            Usage: {colorama.Fore.LIGHTMAGENTA_EX}{exe} COMMAND [OPTIONS]{colorama.Style.RESET_ALL}

            Commands:
              publish  - (re-)publishes ads
              verify   - verifies the configuration files
              delete   - deletes ads
              download - downloads one or multiple ads
              --
              help     - displays this help (default command)
              version  - displays the application version

            Options:
              --ads=all|due|new|<id(s)> (publish) - specifies which ads to (re-)publish (DEFAULT: due)
                    Possible values:
                    * all: (re-)publish all ads ignoring republication_interval
                    * due: publish all new ads and republish ads according the republication_interval
                    * new: only publish new ads (i.e. ads that have no id in the config file)
                    * <id(s)>: provide one or several ads by ID to (re-)publish, like e.g. "--ads=1,2,3" ignoring republication_interval
              --ads=all|new|<id(s)> (download) - specifies which ads to download (DEFAULT: new)
                    Possible values:
                    * all: downloads all ads from your profile
                    * new: downloads ads from your profile that are not locally saved yet
                    * <id(s)>: provide one or several ads by ID to download, like e.g. "--ads=1,2,3"
              --force           - alias for '--ads=all'
              --keep-old        - don't delete old ads on republication
              --config=<PATH>   - path to the config YAML or JSON file (DEFAULT: ./config.yaml)
              --logfile=<PATH>  - path to the logfile (DEFAULT: ./kleinanzeigen-bot.log)
              --lang=en|de      - display language (STANDARD: system language if supported, otherwise English)
              -v, --verbose     - enables verbose output - only useful when troubleshooting issues
            """.rstrip()))

    def parse_args(self, args:list[str]) -> None:
        try:
            options, arguments = getopt.gnu_getopt(args[1:], "hv", [
                "ads=",
                "config=",
                "force",
                "help",
                "keep-old",
                "logfile=",
                "lang=",
                "verbose"
            ])
        except getopt.error as ex:
            LOG.error(ex.msg)
            LOG.error("Use --help to display available options.")
            sys.exit(2)

        for option, value in options:
            match option:
                case "-h" | "--help":
                    self.show_help()
                    sys.exit(0)
                case "--config":
                    self.config_file_path = abspath(value)
                case "--logfile":
                    if value:
                        self.log_file_path = abspath(value)
                    else:
                        self.log_file_path = None
                case "--ads":
                    self.ads_selector = value.strip().lower()
                case "--force":
                    self.ads_selector = "all"
                case "--keep-old":
                    self.keep_old_ads = True
                case "--lang":
                    set_current_locale(Locale.of(value))
                case "-v" | "--verbose":
                    LOG.setLevel(logging.DEBUG)
                    logging.getLogger("nodriver").setLevel(logging.INFO)

        match len(arguments):
            case 0:
                self.command = "help"
            case 1:
                self.command = arguments[0]
            case _:
                LOG.error("More than one command given: %s", arguments)
                sys.exit(2)

    def configure_file_logging(self) -> None:
        if not self.log_file_path:
            return
        if self.file_log:
            return

        LOG.info("Logging to [%s]...", self.log_file_path)
        self.file_log = RotatingFileHandler(filename = self.log_file_path, maxBytes = 10 * 1024 * 1024, backupCount = 10, encoding = "utf-8")
        self.file_log.setLevel(logging.DEBUG)
        self.file_log.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        LOG_ROOT.addHandler(self.file_log)

        LOG.info("App version: %s", self.get_version())
        LOG.info("Python version: %s", sys.version)

    def load_ads(self, *, ignore_inactive:bool = True, check_id:bool = True) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
        LOG.info("Searching for ad config files...")

        ad_files:dict[str, str] = {}
        data_root_dir = os.path.dirname(self.config_file_path)
        for file_pattern in self.config["ad_files"]:
            for ad_file in glob.glob(file_pattern, root_dir = data_root_dir, flags = glob.GLOBSTAR | glob.BRACE | glob.EXTGLOB):
                if not str(ad_file).endswith('ad_fields.yaml'):
                    ad_files[abspath(ad_file, relative_to = data_root_dir)] = ad_file
        LOG.info(" -> found %s", pluralize("ad config file", ad_files))
        if not ad_files:
            return []

        descr_prefix = self.config["ad_defaults"]["description"]["prefix"] or ""
        descr_suffix = self.config["ad_defaults"]["description"]["suffix"] or ""

        ids = []
        use_specific_ads = False
        if re.compile(r'\d+[,\d+]*').search(self.ads_selector):
            ids = [int(n) for n in self.ads_selector.split(',')]
            use_specific_ads = True
            LOG.info('Start fetch task for the ad(s) with id(s):')
            LOG.info(' | '.join([str(id_) for id_ in ids]))

        ad_fields = utils.load_dict_from_module(resources, "ad_fields.yaml")
        ads = []
        for ad_file, ad_file_relative in sorted(ad_files.items()):
            ad_cfg_orig = utils.load_dict(ad_file, "ad")
            ad_cfg = copy.deepcopy(ad_cfg_orig)
            apply_defaults(ad_cfg, self.config["ad_defaults"], ignore = lambda k, _: k == "description", override = lambda _, v: v == "")
            apply_defaults(ad_cfg, ad_fields)

            if ignore_inactive and not ad_cfg["active"]:
                LOG.info(" -> SKIPPED: inactive ad [%s]", ad_file_relative)
                continue

            if use_specific_ads:
                if ad_cfg["id"] not in ids:
                    LOG.info(" -> SKIPPED: ad [%s] is not in list of given ids.", ad_file_relative)
                    continue
            else:
                if self.ads_selector == "new" and ad_cfg["id"] and check_id:
                    LOG.info(" -> SKIPPED: ad [%s] is not new. already has an id assigned.", ad_file_relative)
                    continue

                if self.ads_selector == "due":
                    if ad_cfg["updated_on"]:
                        last_updated_on = parse_datetime(ad_cfg["updated_on"])
                    elif ad_cfg["created_on"]:
                        last_updated_on = parse_datetime(ad_cfg["created_on"])
                    else:
                        last_updated_on = None

                    if last_updated_on:
                        ad_age = datetime.utcnow() - last_updated_on
                        if ad_age.days <= ad_cfg["republication_interval"]:
                            LOG.info(" -> SKIPPED: ad [%s] was last published %d days ago. republication is only required every %s days",
                                ad_file_relative,
                                ad_age.days,
                                ad_cfg["republication_interval"]
                            )
                            continue

            ad_cfg["description"] = descr_prefix + (ad_cfg["description"] or "") + descr_suffix
            ad_cfg["description"] = ad_cfg["description"].replace("@", "(at)")
            ensure(len(ad_cfg["description"]) <= 4000, f"Length of ad description including prefix and suffix exceeds 4000 chars. @ [{ad_file}]")

            # pylint: disable=cell-var-from-loop
            def assert_one_of(path:str, allowed:Iterable[str]) -> None:
                ensure(safe_get(ad_cfg, *path.split(".")) in allowed, f"-> property [{path}] must be one of: {allowed} @ [{ad_file}]")

            def assert_min_len(path:str, minlen:int) -> None:
                ensure(len(safe_get(ad_cfg, *path.split("."))) >= minlen, f"-> property [{path}] must be at least {minlen} characters long @ [{ad_file}]")

            def assert_has_value(path:str) -> None:
                ensure(safe_get(ad_cfg, *path.split(".")), f"-> property [{path}] not specified @ [{ad_file}]")
            # pylint: enable=cell-var-from-loop

            assert_one_of("type", {"OFFER", "WANTED"})
            assert_min_len("title", 10)
            assert_has_value("description")
            assert_one_of("price_type", {"FIXED", "NEGOTIABLE", "GIVE_AWAY", "NOT_APPLICABLE"})
            if ad_cfg["price_type"] == "GIVE_AWAY":
                ensure(not safe_get(ad_cfg, "price"), f"-> [price] must not be specified for GIVE_AWAY ad @ [{ad_file}]")
            elif ad_cfg["price_type"] == "FIXED":
                assert_has_value("price")

            assert_one_of("shipping_type", {"PICKUP", "SHIPPING", "NOT_APPLICABLE"})
            assert_has_value("contact.name")
            assert_has_value("republication_interval")

            if ad_cfg["id"]:
                ad_cfg["id"] = int(ad_cfg["id"])

            if ad_cfg["category"]:
                resolved_category_id = self.categories.get(ad_cfg["category"])
                if not resolved_category_id and ">" in ad_cfg["category"]:
                    # this maps actually to the sonstiges/weiteres sub-category
                    parent_category = ad_cfg["category"].rpartition(">")[0].strip()
                    resolved_category_id = self.categories.get(parent_category)
                    if resolved_category_id:
                        LOG.warning(
                            "Category [%s] unknown. Using category [%s] with ID [%s] instead.",
                            ad_cfg["category"], parent_category, resolved_category_id)

                if resolved_category_id:
                    ad_cfg["category"] = resolved_category_id

            if ad_cfg["shipping_costs"]:
                ad_cfg["shipping_costs"] = str(round(utils.parse_decimal(ad_cfg["shipping_costs"]), 2))

            if ad_cfg["images"]:
                images = []
                ad_dir = os.path.dirname(ad_file)
                for image_pattern in ad_cfg["images"]:
                    pattern_images = set()
                    for image_file in glob.glob(image_pattern, root_dir = ad_dir, flags = glob.GLOBSTAR | glob.BRACE | glob.EXTGLOB):
                        _, image_file_ext = os.path.splitext(image_file)
                        ensure(image_file_ext.lower() in {".gif", ".jpg", ".jpeg", ".png"}, f"Unsupported image file type [{image_file}]")
                        if os.path.isabs(image_file):
                            pattern_images.add(image_file)
                        else:
                            pattern_images.add(abspath(image_file, relative_to = ad_file))
                    images.extend(sorted(pattern_images))
                ensure(images or not ad_cfg["images"], f"No images found for given file patterns {ad_cfg['images']} at {ad_dir}")
                ad_cfg["images"] = list(dict.fromkeys(images))

            ads.append((
                ad_file,
                ad_cfg,
                ad_cfg_orig
            ))

        LOG.info("Loaded %s", pluralize("ad", ads))
        return ads

    def load_config(self) -> None:
        config_defaults = utils.load_dict_from_module(resources, "config_defaults.yaml")
        config = utils.load_dict_if_exists(self.config_file_path, _("config"))

        if config is None:
            LOG.warning("Config file %s does not exist. Creating it with default values...", self.config_file_path)
            utils.save_dict(self.config_file_path, config_defaults)
            config = {}

        self.config = apply_defaults(config, config_defaults)

        self.categories = utils.load_dict_from_module(resources, "categories.yaml", "categories")
        deprecated_categories = utils.load_dict_from_module(resources, "categories_old.yaml", "categories")
        self.categories.update(deprecated_categories)
        if self.config["categories"]:
            self.categories.update(self.config["categories"])
        LOG.info(" -> found %s", pluralize("category", self.categories))

        ensure(self.config["login"]["username"], f"[login.username] not specified @ [{self.config_file_path}]")
        ensure(self.config["login"]["password"], f"[login.password] not specified @ [{self.config_file_path}]")

        self.browser_config.arguments = self.config["browser"]["arguments"]
        self.browser_config.binary_location = self.config["browser"]["binary_location"]
        self.browser_config.extensions = [abspath(item, relative_to = self.config_file_path) for item in self.config["browser"]["extensions"]]
        self.browser_config.use_private_window = self.config["browser"]["use_private_window"]
        if self.config["browser"]["user_data_dir"]:
            self.browser_config.user_data_dir = abspath(self.config["browser"]["user_data_dir"], relative_to = self.config_file_path)
        self.browser_config.profile_name = self.config["browser"]["profile_name"]

    async def login(self) -> None:
        LOG.info("Checking if already logged in...")
        await self.web_open(f"{self.root_url}")

        if await self.is_logged_in():
            LOG.info("Already logged in as [%s]. Skipping login.", self.config["login"]["username"])
            return

        LOG.info("Opening login page...")
        await self.web_open(f"{self.root_url}/m-einloggen.html?targetUrl=/")

        try:
            await self.web_find(By.CSS_SELECTOR, "iframe[src*='captcha-delivery.com']", timeout = 2)
            LOG.warning("############################################")
            LOG.warning("# Captcha present! Please solve the captcha.")
            LOG.warning("############################################")
            await self.web_await(lambda: self.web_find(By.ID, "login-form") is not None, timeout = 5 * 60)
        except TimeoutError:
            pass

        await self.fill_login_data_and_send()
        await self.handle_after_login_logic()

        # Sometimes a second login is required
        if not await self.is_logged_in():
            await self.fill_login_data_and_send()
            await self.handle_after_login_logic()

    async def fill_login_data_and_send(self) -> None:
        LOG.info("Logging in as [%s]...", self.config["login"]["username"])
        await self.web_input(By.ID, "email", self.config["login"]["username"])
        await self.web_input(By.ID, "password", self.config["login"]["password"])
        await self.web_click(By.CSS_SELECTOR, "form#login-form button[type='submit']")

    async def handle_after_login_logic(self) -> None:
        try:
            await self.web_find(By.TEXT, "Wir haben dir gerade einen 6-stelligen Code für die Telefonnummer", timeout = 4)
            LOG.warning("############################################")
            LOG.warning("# Device verification message detected. Please follow the instruction displayed in the Browser.")
            LOG.warning("############################################")
            await ainput("Press ENTER when done...")
        except TimeoutError:
            pass

        try:
            LOG.info("Handling GDPR disclaimer...")
            await self.web_find(By.ID, "gdpr-banner-accept", timeout = 10)
            await self.web_click(By.ID, "gdpr-banner-cmp-button")
            await self.web_click(By.CSS_SELECTOR, "#ConsentManagementPage button.Button-secondary", timeout = 10)
        except TimeoutError:
            pass

    async def is_logged_in(self) -> bool:
        try:
            user_info = await self.web_text(By.ID, "user-email")
            if self.config['login']['username'].lower() in user_info.lower():
                return True
        except TimeoutError:
            return False
        return False

    async def delete_ads(self, ad_cfgs:list[tuple[str, dict[str, Any], dict[str, Any]]]) -> None:
        count = 0

        published_ads = json.loads(
            (await self.web_request(f"{self.root_url}/m-meine-anzeigen-verwalten.json?sort=DEFAULT"))["content"])["ads"]

        for (ad_file, ad_cfg, _) in ad_cfgs:
            count += 1
            LOG.info("Processing %s/%s: '%s' from [%s]...", count, len(ad_cfgs), ad_cfg["title"], ad_file)
            await self.delete_ad(ad_cfg, self.config["publishing"]["delete_old_ads_by_title"], published_ads)
            await self.web_sleep()

        LOG.info("############################################")
        LOG.info("DONE: Deleted %s", pluralize("ad", count))
        LOG.info("############################################")

    async def delete_ad(self, ad_cfg: dict[str, Any], delete_old_ads_by_title: bool, published_ads: list[dict[str, Any]]) -> bool:
        LOG.info("Deleting ad '%s' if already present...", ad_cfg["title"])

        await self.web_open(f"{self.root_url}/m-meine-anzeigen.html")
        csrf_token_elem = await self.web_find(By.CSS_SELECTOR, "meta[name=_csrf]")
        csrf_token = csrf_token_elem.attrs["content"]
        ensure(csrf_token is not None, "Expected CSRF Token not found in HTML content!")

        if delete_old_ads_by_title:

            for published_ad in published_ads:
                published_ad_id = int(published_ad.get("id", -1))
                published_ad_title = published_ad.get("title", "")
                if ad_cfg["id"] == published_ad_id or ad_cfg["title"] == published_ad_title:
                    LOG.info(" -> deleting %s '%s'...", published_ad_id, published_ad_title)
                    await self.web_request(
                        url = f"{self.root_url}/m-anzeigen-loeschen.json?ids={published_ad_id}",
                        method = "POST",
                        headers = {"x-csrf-token": csrf_token}
                    )
        elif ad_cfg["id"]:
            await self.web_request(
                url = f"{self.root_url}/m-anzeigen-loeschen.json?ids={ad_cfg['id']}",
                method = "POST",
                headers = {"x-csrf-token": csrf_token},
                valid_response_codes = [200, 404]
            )

        await self.web_sleep()
        ad_cfg["id"] = None
        return True

    async def publish_ads(self, ad_cfgs:list[tuple[str, dict[str, Any], dict[str, Any]]]) -> None:
        count = 0

        published_ads = json.loads(
            (await self.web_request(f"{self.root_url}/m-meine-anzeigen-verwalten.json?sort=DEFAULT"))["content"])["ads"]

        for (ad_file, ad_cfg, ad_cfg_orig) in ad_cfgs:
            LOG.info("Processing %s/%s: '%s' from [%s]...", count + 1, len(ad_cfgs), ad_cfg["title"], ad_file)

            if [x for x in published_ads if x["id"] == ad_cfg["id"] and x["state"] == "paused"]:
                LOG.info("Skipping because ad is reserved")
                continue

            count += 1

            await self.publish_ad(ad_file, ad_cfg, ad_cfg_orig, published_ads)
            await self.web_await(lambda: self.web_check(By.ID, "checking-done", Is.DISPLAYED), timeout = 5 * 60)

            if self.config["publishing"]["delete_old_ads"] == "AFTER_PUBLISH" and not self.keep_old_ads:
                await self.delete_ad(ad_cfg, False, published_ads)

        LOG.info("############################################")
        LOG.info("DONE: (Re-)published %s", pluralize("ad", count))
        LOG.info("############################################")

    async def publish_ad(self, ad_file:str, ad_cfg: dict[str, Any], ad_cfg_orig: dict[str, Any], published_ads: list[dict[str, Any]]) -> None:
        """
        @param ad_cfg: the effective ad config (i.e. with default values applied etc.)
        @param ad_cfg_orig: the ad config as present in the YAML file
        @param published_ads: json list of published ads
        """
        await self.assert_free_ad_limit_not_reached()

        if self.config["publishing"]["delete_old_ads"] == "BEFORE_PUBLISH" and not self.keep_old_ads:
            await self.delete_ad(ad_cfg, self.config["publishing"]["delete_old_ads_by_title"], published_ads)

        LOG.info("Publishing ad '%s'...", ad_cfg["title"])

        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(" -> effective ad meta:")
            YAML().dump(ad_cfg, sys.stdout)

        await self.web_open(f"{self.root_url}/p-anzeige-aufgeben-schritt2.html")

        if ad_cfg["type"] == "WANTED":
            await self.web_click(By.ID, "adType2")

        #############################
        # set title
        #############################
        await self.web_input(By.ID, "postad-title", ad_cfg["title"])

        #############################
        # set category
        #############################
        await self.__set_category(ad_cfg['category'], ad_file)

        #############################
        # set special attributes
        #############################
        await self.__set_special_attributes(ad_cfg)

        #############################
        # set shipping type/options/costs
        #############################
        if ad_cfg["type"] == "WANTED":
            # special handling for ads of type WANTED since shipping is a special attribute for these
            if ad_cfg["shipping_type"] in {"PICKUP", "SHIPPING"}:
                shipping_value = "ja" if ad_cfg["shipping_type"] == "SHIPPING" else "nein"
                try:
                    await self.web_select(By.XPATH, "//select[contains(@id, '.versand_s')]", shipping_value)
                except TimeoutError:
                    LOG.warning("Failed to set shipping attribute for type '%s'!", ad_cfg['shipping_type'])
        elif ad_cfg["shipping_type"] == "PICKUP":
            try:
                await self.web_click(By.XPATH,
                    '//*[contains(@class, "ShippingPickupSelector")]//label[text()[contains(.,"Nur Abholung")]]/../input[@type="radio"]')
            except TimeoutError as ex:
                LOG.debug(ex, exc_info = True)
        elif ad_cfg["shipping_options"]:
            await self.web_click(By.XPATH, '//button[contains(@aria-label, "Dialog mit Optionen öffnen")]')
            await self.web_click(By.CSS_SELECTOR, '[class*="CarrierSelectionModal--Button"]')
            await self.__set_shipping_options(ad_cfg)
        else:
            try:
                await self.web_click(By.CSS_SELECTOR, '[class*="jsx-2623555103"]')
                await self.web_click(By.CSS_SELECTOR, '[class*="CarrierSelectionModal--Button"]')
                await self.web_click(By.CSS_SELECTOR, '[class*="CarrierOption--Main"]')
                if ad_cfg["shipping_costs"]:
                    await self.web_input(By.CSS_SELECTOR, '.IndividualShippingInput input[type="text"]', str.replace(ad_cfg["shipping_costs"], ".", ",")
                    )
                await self.web_click(By.XPATH, '//*[contains(@class, "ModalDialog--Actions")]//button[.//*[text()[contains(.,"Fertig")]]]')
            except TimeoutError as ex:
                LOG.debug(ex, exc_info = True)

        #############################
        # set price
        #############################
        price_type = ad_cfg["price_type"]
        if price_type != "NOT_APPLICABLE":
            try:
                await self.web_select(By.CSS_SELECTOR, "select#price-type-react, select#micro-frontend-price-type, select#priceType", price_type)
            except TimeoutError:
                pass
            if safe_get(ad_cfg, "price"):
                await self.web_input(By.CSS_SELECTOR, "input#post-ad-frontend-price, input#micro-frontend-price, input#pstad-price", ad_cfg["price"])

        #############################
        # set sell_directly
        #############################
        sell_directly = ad_cfg["sell_directly"]
        try:
            if ad_cfg["shipping_type"] == "SHIPPING":
                if sell_directly and ad_cfg["shipping_options"] and price_type in {"FIXED", "NEGOTIABLE"}:
                    if not await self.web_check(By.ID, "radio-buy-now-yes", Is.SELECTED):
                        await self.web_click(By.ID, 'radio-buy-now-yes')
                elif not await self.web_check(By.ID, "radio-buy-now-no", Is.SELECTED):
                    await self.web_click(By.ID, 'radio-buy-now-no')
        except TimeoutError as ex:
            LOG.debug(ex, exc_info = True)

        #############################
        # set description
        #############################
        await self.web_execute("document.querySelector('#pstad-descrptn').value = `" + ad_cfg["description"].replace("`", "'") + "`")

        #############################
        # set contact zipcode
        #############################
        if ad_cfg["contact"]["zipcode"]:
            await self.web_input(By.ID, "pstad-zip", ad_cfg["contact"]["zipcode"])

        #############################
        # set contact street
        #############################
        if ad_cfg["contact"]["street"]:
            try:
                if await self.web_check(By.ID, "pstad-street", Is.DISABLED):
                    await self.web_click(By.ID, "addressVisibility")
                    await self.web_sleep()
            except TimeoutError:
                # ignore
                pass
            await self.web_input(By.ID, "pstad-street", ad_cfg["contact"]["street"])

        #############################
        # set contact name
        #############################
        if ad_cfg["contact"]["name"] and not await self.web_check(By.ID, "postad-contactname", Is.READONLY):
            await self.web_input(By.ID, "postad-contactname", ad_cfg["contact"]["name"])

        #############################
        # set contact phone
        #############################
        if ad_cfg["contact"]["phone"]:
            if await self.web_check(By.ID, "postad-phonenumber", Is.DISPLAYED):
                try:
                    if await self.web_check(By.ID, "postad-phonenumber", Is.DISABLED):
                        await self.web_click(By.ID, "phoneNumberVisibility")
                        await self.web_sleep()
                except TimeoutError:
                    # ignore
                    pass
                await self.web_input(By.ID, "postad-phonenumber", ad_cfg["contact"]["phone"])

        #############################
        # upload images
        #############################
        await self.__upload_images(ad_cfg)

        #############################
        # wait for captcha
        #############################
        try:
            await self.web_find(By.CSS_SELECTOR, "iframe[name^='a-'][src^='https://www.google.com/recaptcha/api2/anchor?']", timeout = 2)
            LOG.warning("############################################")
            LOG.warning("# Captcha present! Please solve the captcha.")
            LOG.warning("############################################")
            await self.web_scroll_page_down()
            input(_("Press a key to continue..."))
        except TimeoutError:
            pass

        #############################
        # submit
        #############################
        try:
            await self.web_click(By.ID, "pstad-submit")
        except TimeoutError:
            # https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/40
            await self.web_click(By.XPATH, "//fieldset[@id='postad-publish']//*[contains(text(),'Anzeige aufgeben')]")
            await self.web_click(By.ID, "imprint-guidance-submit")

        # check for no image question
        image_hint_xpath = '//*[contains(@class, "ModalDialog--Actions")]//button[.//*[text()[contains(.,"Ohne Bild veröffentlichen")]]]'
        if not ad_cfg["images"] and await self.web_check(By.XPATH, image_hint_xpath, Is.DISPLAYED):
            await self.web_click(By.XPATH, image_hint_xpath)

        await self.web_await(lambda: "p-anzeige-aufgeben-bestaetigung.html?adId=" in self.page.url, timeout = 20)

        ad_cfg_orig["updated_on"] = datetime.utcnow().isoformat()
        if not ad_cfg["created_on"] and not ad_cfg["id"]:
            ad_cfg_orig["created_on"] = ad_cfg_orig["updated_on"]

        # extract the ad id from the URL's query parameter
        current_url_query_params = urllib_parse.parse_qs(urllib_parse.urlparse(self.page.url).query)
        ad_id = int(current_url_query_params.get("adId", [])[0])
        ad_cfg_orig["id"] = ad_id

        LOG.info(" -> SUCCESS: ad published with ID %s", ad_id)

        utils.save_dict(ad_file, ad_cfg_orig)

    async def __set_condition(self, condition_value: str) -> None:
        condition_mapping = {
            "new_with_tag": "Neu mit Etikett",
            "new": "Neu",
            "like_new": "Sehr Gut",
            "alright": "Gut",
            "ok": "In Ordnung",
        }
        mapped_condition = condition_mapping.get(condition_value)

        try:
            # Open condition dialog
            await self.web_click(By.CSS_SELECTOR, '[class*="ConditionSelector"] button')
        except TimeoutError:
            LOG.debug("Unable to open condition dialog and select condition [%s]", condition_value, exc_info = True)
            return

        try:
            # Click radio button
            await self.web_click(By.CSS_SELECTOR, f'.SingleSelectionItem--Main input[type=radio][data-testid="{mapped_condition}"]')
        except TimeoutError:
            LOG.debug("Unable to select condition [%s]", condition_value, exc_info = True)

        try:
            # Click continue button
            await self.web_click(By.XPATH, '//*[contains(@class, "ModalDialog--Actions")]//button[.//*[text()[contains(.,"Bestätigen")]]]')
        except TimeoutError as ex:
            raise TimeoutError(_("Unable to close condition dialog!")) from ex

    async def __set_category(self, category: str | None, ad_file:str) -> None:
        # click on something to trigger automatic category detection
        await self.web_click(By.ID, "pstad-descrptn")

        is_category_auto_selected = False
        try:
            if await self.web_text(By.ID, "postad-category-path"):
                is_category_auto_selected = True
        except TimeoutError:
            pass

        if category:
            await self.web_sleep()  # workaround for https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/39
            await self.web_click(By.ID, "pstad-lnk-chngeCtgry")
            await self.web_find(By.ID, "postad-step1-sbmt")

            category_url = f"{self.root_url}/p-kategorie-aendern.html#?path={category}"
            await self.web_open(category_url)
            await self.web_click(By.XPATH, "//*[@id='postad-step1-sbmt']/button")
        else:
            ensure(is_category_auto_selected, f"No category specified in [{ad_file}] and automatic category detection failed")

    async def __set_special_attributes(self, ad_cfg: dict[str, Any]) -> None:
        if ad_cfg["special_attributes"]:
            LOG.debug('Found %i special attributes', len(ad_cfg["special_attributes"]))
            for special_attribute_key, special_attribute_value in ad_cfg["special_attributes"].items():

                if special_attribute_key == "condition_s":
                    await self.__set_condition(special_attribute_value)
                    continue

                LOG.debug("Setting special attribute [%s] to [%s]...", special_attribute_key, special_attribute_value)
                try:
                    # if the <select> element exists but is inside an invisible container, make the container visible
                    select_container_xpath = f"//div[@class='l-row' and descendant::select[@id='{special_attribute_key}']]"
                    if not await self.web_check(By.XPATH, select_container_xpath, Is.DISPLAYED):
                        await (await self.web_find(By.XPATH, select_container_xpath)).apply("elem => elem.singleNodeValue.style.display = 'block'")
                except TimeoutError:
                    pass  # nosec

                try:
                    # finding element by name cause id are composed sometimes eg. autos.marke_s+autos.model_s for Modell by cars
                    special_attr_elem = await self.web_find(By.XPATH, f"//*[contains(@name, '{special_attribute_key}')]")
                except TimeoutError as ex:
                    LOG.debug("Attribute field '%s' could not be found.", special_attribute_key)
                    raise TimeoutError(f"Failed to set special attribute [{special_attribute_key}] (not found)") from ex

                # workaround for https://github.com/Second-Hand-Friends/kleinanzeigen-bot/issues/368
                elem_id = getattr(special_attr_elem.attrs, 'id')
                elem_id = ''.join(['\\' + c if c in '!"#$%&\'()*+,./:;<=>?@[\\]^`{|}~' else c for c in elem_id])

                try:
                    if special_attr_elem.local_name == 'select':
                        LOG.debug("Attribute field '%s' seems to be a select...", special_attribute_key)
                        await self.web_select(By.ID, elem_id, special_attribute_value)
                    elif getattr(special_attr_elem.attrs, 'type') == 'checkbox':
                        LOG.debug("Attribute field '%s' seems to be a checkbox...", special_attribute_key)
                        await self.web_click(By.ID, elem_id)
                    else:
                        LOG.debug("Attribute field '%s' seems to be a text input...", special_attribute_key)
                        await self.web_input(By.ID, elem_id, special_attribute_value)
                except TimeoutError as ex:
                    LOG.debug("Attribute field '%s' is not of kind radio button.", special_attribute_key)
                    raise TimeoutError(f"Failed to set special attribute [{special_attribute_key}]") from ex
                LOG.debug("Successfully set attribute field [%s] to [%s]...", special_attribute_key, special_attribute_value)

    async def __set_shipping_options(self, ad_cfg: dict[str, Any]) -> None:
        shipping_options_mapping = {
            "DHL_2": ("Klein", "Paket 2 kg"),
            "Hermes_Päckchen": ("Klein", "Päckchen"),
            "Hermes_S": ("Klein", "S-Paket"),
            "DHL_5": ("Mittel", "Paket 5 kg"),
            "Hermes_M": ("Mittel", "M-Paket"),
            "DHL_10": ("Groß", "Paket 10 kg"),
            "DHL_31,5": ("Groß", "Paket 31,5 kg"),
            "Hermes_L": ("Groß", "L-Paket"),
        }
        try:
            mapped_shipping_options = [
                shipping_options_mapping[option]
                for option in set(ad_cfg["shipping_options"])
            ]
        except KeyError as ex:
            raise KeyError(f"Unknown shipping option(s), please refer to the documentation/README: {ad_cfg['shipping_options']}") from ex

        shipping_sizes, shipping_packages = zip(*mapped_shipping_options)

        try:
            shipping_size, = set(shipping_sizes)
        except ValueError as ex:
            raise ValueError("You can only specify shipping options for one package size!") from ex

        try:
            shipping_size_radio = await self.web_find(By.CSS_SELECTOR, f'.SingleSelectionItem--Main input[type=radio][data-testid="{shipping_size}"]')
            shipping_size_radio_is_checked = hasattr(shipping_size_radio.attrs, "checked")

            if shipping_size_radio_is_checked:
                await self.web_click(
                    By.XPATH,
                    '//*[contains(@class, "ModalDialog--Actions")]'
                    '//*[contains(@class, "Button-primary") and .//*[text()[contains(.,"Weiter")]]]')

                unwanted_shipping_packages = [
                    package for size, package in shipping_options_mapping.values()
                    if size == shipping_size and package not in shipping_packages
                ]
                to_be_clicked_shipping_packages = unwanted_shipping_packages
            else:
                await self.web_click(By.CSS_SELECTOR, f'.SingleSelectionItem--Main input[type=radio][data-testid="{shipping_size}"]')
                to_be_clicked_shipping_packages = list(shipping_packages)

            for shipping_package in to_be_clicked_shipping_packages:
                try:
                    await self.web_click(
                        By.XPATH,
                        '//*[contains(@class, "CarrierSelectionModal")]'
                        '//*[contains(@class, "CarrierOption")]'
                        f'//*[contains(@class, "CarrierOption--Main") and @data-testid="{shipping_package}"]'
                    )
                except TimeoutError as ex:
                    LOG.debug(ex, exc_info = True)

            await self.web_click(By.XPATH, '//*[contains(@class, "ModalDialog--Actions")]//button[.//*[text()[contains(.,"Fertig")]]]')
        except TimeoutError as ex:
            LOG.debug(ex, exc_info = True)

    async def __upload_images(self, ad_cfg: dict[str, Any]) -> None:
        LOG.info(" -> found %s", pluralize("image", ad_cfg["images"]))
        image_upload:Element = await self.web_find(By.CSS_SELECTOR, "input[type=file]")

        for image in ad_cfg["images"]:
            LOG.info(" -> uploading image [%s]", image)
            await image_upload.send_file(image)
            await self.web_sleep()

    async def assert_free_ad_limit_not_reached(self) -> None:
        try:
            await self.web_find(By.XPATH, '/html/body/div[1]/form/fieldset[6]/div[1]/header', timeout = 2)
            raise AssertionError(f"Cannot publish more ads. The monthly limit of free ads of account {self.config['login']['username']} is reached.")
        except TimeoutError:
            pass

    async def download_ads(self) -> None:
        """
        Determines which download mode was chosen with the arguments, and calls the specified download routine.
        This downloads either all, only unsaved (new), or specific ads given by ID.
        """

        ad_extractor = extract.AdExtractor(self.browser, self.config)

        # use relevant download routine
        if self.ads_selector in {'all', 'new'}:  # explore ads overview for these two modes
            LOG.info('Scanning your ad overview...')
            own_ad_urls = await ad_extractor.extract_own_ads_urls()
            LOG.info('%s found.', pluralize("ad", len(own_ad_urls)))

            if self.ads_selector == 'all':  # download all of your adds
                LOG.info('Starting download of all ads...')

                success_count = 0
                # call download function for each ad page
                for add_url in own_ad_urls:
                    ad_id = ad_extractor.extract_ad_id_from_ad_url(add_url)
                    if await ad_extractor.naviagte_to_ad_page(add_url):
                        await ad_extractor.download_ad(ad_id)
                        success_count += 1
                LOG.info("%d of %d ads were downloaded from your profile.", success_count, len(own_ad_urls))

            elif self.ads_selector == 'new':  # download only unsaved ads
                # check which ads already saved
                saved_ad_ids = []
                ads = self.load_ads(ignore_inactive = False, check_id = False)  # do not skip because of existing IDs
                for ad in ads:
                    ad_id = int(ad[2]['id'])
                    saved_ad_ids.append(ad_id)

                # determine ad IDs from links
                ad_id_by_url = {url:ad_extractor.extract_ad_id_from_ad_url(url) for url in own_ad_urls}

                LOG.info("Starting download of not yet downloaded ads...")
                new_count = 0
                for ad_url, ad_id in ad_id_by_url.items():
                    # check if ad with ID already saved
                    if ad_id in saved_ad_ids:
                        LOG.info('The ad with id %d has already been saved.', ad_id)
                        continue

                    if await ad_extractor.naviagte_to_ad_page(ad_url):
                        await ad_extractor.download_ad(ad_id)
                        new_count += 1
                LOG.info('%s were downloaded from your profile.', pluralize("new ad", new_count))

        elif re.compile(r'\d+[,\d+]*').search(self.ads_selector):  # download ad(s) with specific id(s)
            ids = [int(n) for n in self.ads_selector.split(',')]
            LOG.info('Starting download of ad(s) with the id(s):')
            LOG.info(' | '.join([str(ad_id) for ad_id in ids]))

            for ad_id in ids:  # call download routine for every id
                exists = await ad_extractor.naviagte_to_ad_page(ad_id)
                if exists:
                    await ad_extractor.download_ad(ad_id)
                    LOG.info('Downloaded ad with id %d', ad_id)
                else:
                    LOG.error('The page with the id %d does not exist!', ad_id)


#############################
# main entry point
#############################
def main(args:list[str]) -> None:
    if "version" not in args:
        print(textwrap.dedent(r"""
         _    _      _                           _                       _           _
        | | _| | ___(_)_ __   __ _ _ __  _______(_) __ _  ___ _ __      | |__   ___ | |_
        | |/ / |/ _ \ | '_ \ / _` | '_ \|_  / _ \ |/ _` |/ _ \ '_ \ ____| '_ \ / _ \| __|
        |   <| |  __/ | | | | (_| | | | |/ /  __/ | (_| |  __/ | | |____| |_) | (_) | |_
        |_|\_\_|\___|_|_| |_|\__,_|_| |_/___\___|_|\__, |\___|_| |_|    |_.__/ \___/ \__|
                                                   |___/
                                 https://github.com/Second-Hand-Friends/kleinanzeigen-bot
        """)[1:], flush = True)  # [1:] removes the first empty blank line

    utils.configure_console_logging()

    signal.signal(signal.SIGINT, utils.on_sigint)  # capture CTRL+C
    sys.excepthook = utils.on_exception
    atexit.register(utils.on_exit)

    bot = KleinanzeigenBot()
    atexit.register(bot.close_browser_session)
    nodriver.loop().run_until_complete(bot.run(args))


if __name__ == "__main__":
    utils.configure_console_logging()
    LOG.error("Direct execution not supported. Use 'pdm run app'")
    sys.exit(1)
