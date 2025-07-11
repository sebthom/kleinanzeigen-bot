# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
import json, mimetypes, os, shutil  # isort: skip
import urllib.request as urllib_request
from datetime import datetime
from typing import Any, Final

from kleinanzeigen_bot.model.ad_model import ContactPartial

from .model.ad_model import AdPartial
from .model.config_model import Config
from .utils import dicts, i18n, loggers, misc, reflect
from .utils.web_scraping_mixin import Browser, By, Element, WebScrapingMixin

__all__ = [
    "AdExtractor",
]

LOG:Final[loggers.Logger] = loggers.get_logger(__name__)


class AdExtractor(WebScrapingMixin):
    """
    Wrapper class for ad extraction that uses an active bot´s browser session to extract specific elements from an ad page.
    """

    def __init__(self, browser:Browser, config:Config) -> None:
        super().__init__()
        self.browser = browser
        self.config = config

    async def download_ad(self, ad_id:int) -> None:
        """
        Downloads an ad to a specific location, specified by config and ad ID.
        NOTE: Requires that the driver session currently is on the ad page.

        :param ad_id: the ad ID
        """

        # create sub-directory for ad(s) to download (if necessary):
        relative_directory = "downloaded-ads"
        # make sure configured base directory exists
        if not os.path.exists(relative_directory) or not os.path.isdir(relative_directory):
            os.mkdir(relative_directory)
            LOG.info("Created ads directory at ./%s.", relative_directory)

        new_base_dir = os.path.join(relative_directory, f"ad_{ad_id}")
        if os.path.exists(new_base_dir):
            LOG.info("Deleting current folder of ad %s...", ad_id)
            shutil.rmtree(new_base_dir)
        os.mkdir(new_base_dir)
        LOG.info("New directory for ad created at %s.", new_base_dir)

        # call extraction function
        ad_cfg:AdPartial = await self._extract_ad_page_info(new_base_dir, ad_id)
        ad_file_path = new_base_dir + "/" + f"ad_{ad_id}.yaml"
        dicts.save_dict(
            ad_file_path,
            ad_cfg.model_dump(),
            header = "# yaml-language-server: $schema=https://raw.githubusercontent.com/Second-Hand-Friends/kleinanzeigen-bot/refs/heads/main/schemas/ad.schema.json")

    async def _download_images_from_ad_page(self, directory:str, ad_id:int) -> list[str]:
        """
        Downloads all images of an ad.

        :param directory: the path of the directory created for this ad
        :param ad_id: the ID of the ad to download the images from
        :return: the relative paths for all downloaded images
        """

        n_images:int
        img_paths = []
        try:
            # download all images from box
            image_box = await self.web_find(By.CLASS_NAME, "galleryimage-large")

            images = await self.web_find_all(By.CSS_SELECTOR, ".galleryimage-element[data-ix] > img", parent = image_box)
            n_images = len(images)
            LOG.info("Found %s.", i18n.pluralize("image", n_images))

            img_fn_prefix = "ad_" + str(ad_id) + "__img"
            img_nr = 1
            dl_counter = 0

            for img_element in images:
                current_img_url = img_element.attrs["src"]  # URL of the image
                if current_img_url is None:
                    continue

                with urllib_request.urlopen(current_img_url) as response:  # noqa: S310 Audit URL open for permitted schemes.
                    content_type = response.info().get_content_type()
                    file_ending = mimetypes.guess_extension(content_type)
                    img_path = f"{directory}/{img_fn_prefix}{img_nr}{file_ending}"
                    with open(img_path, "wb") as f:
                        shutil.copyfileobj(response, f)
                    dl_counter += 1
                    img_paths.append(img_path.rsplit("/", maxsplit = 1)[-1])

                img_nr += 1
            LOG.info("Downloaded %s.", i18n.pluralize("image", dl_counter))

        except TimeoutError:  # some ads do not require images
            LOG.warning("No image area found. Continuing without downloading images.")

        return img_paths

    def extract_ad_id_from_ad_url(self, url:str) -> int:
        """
        Extracts the ID of an ad, given by its reference link.

        :param url: the URL to the ad page
        :return: the ad ID, a (ten-digit) integer number
        """

        num_part = url.rsplit("/", maxsplit = 1)[-1]  # suffix
        id_part = num_part.split("-", maxsplit = 1)[0]

        try:
            path = url.split("?", maxsplit = 1)[0]  # Remove query string if present
            last_segment = path.rstrip("/").rsplit("/", maxsplit = 1)[-1]  # Get last path component
            id_part = last_segment.split("-", maxsplit = 1)[0]  # Extract part before first hyphen
            return int(id_part)
        except (IndexError, ValueError) as ex:
            LOG.warning("Failed to extract ad ID from URL '%s': %s", url, ex)
            return -1

    async def extract_own_ads_urls(self) -> list[str]:
        """
        Extracts the references to all own ads.

        :return: the links to your ad pages
        """
        # navigate to "your ads" page
        await self.web_open("https://www.kleinanzeigen.de/m-meine-anzeigen.html")
        await self.web_sleep(2000, 3000)  # Consider replacing with explicit waits later

        # Try to find the main ad list container first
        try:
            ad_list_container = await self.web_find(By.ID, "my-manageitems-adlist")
        except TimeoutError:
            LOG.warning("Ad list container #my-manageitems-adlist not found. Maybe no ads present?")
            return []

        # --- Pagination handling ---
        multi_page = False
        try:
            # Correct selector: Use uppercase '.Pagination'
            pagination_section = await self.web_find(By.CSS_SELECTOR, ".Pagination", timeout = 10)  # Increased timeout slightly
            # Correct selector: Use 'aria-label'
            # Also check if the button is actually present AND potentially enabled (though enabled check isn't strictly necessary here, only for clicking later)
            next_buttons = await self.web_find_all(By.CSS_SELECTOR, 'button[aria-label="Nächste"]', parent = pagination_section)
            if next_buttons:
                # Check if at least one 'Nächste' button is not disabled (optional but good practice)
                enabled_next_buttons = [btn for btn in next_buttons if not btn.attrs.get("disabled")]
                if enabled_next_buttons:
                    multi_page = True
                    LOG.info("Multiple ad pages detected.")
                else:
                    LOG.info("Next button found but is disabled. Assuming single effective page.")

            else:
                LOG.info('No "Naechste" button found within pagination. Assuming single page.')
        except TimeoutError:
            # This will now correctly trigger only if the '.Pagination' div itself is not found
            LOG.info("No pagination controls found. Assuming single page.")
        except Exception as e:
            LOG.exception("Error during pagination detection: %s", e)
            LOG.info("Assuming single page due to error during pagination check.")
        # --- End Pagination Handling ---

        refs:list[str] = []
        current_page = 1
        while True:  # Loop reference extraction
            LOG.info("Extracting ads from page %s...", current_page)
            # scroll down to load dynamically if necessary
            await self.web_scroll_page_down()
            await self.web_sleep(2000, 3000)  # Consider replacing with explicit waits

            # Re-find the ad list container on the current page/state
            try:
                ad_list_container = await self.web_find(By.ID, "my-manageitems-adlist")
                list_items = await self.web_find_all(By.CLASS_NAME, "cardbox", parent = ad_list_container)
                LOG.info("Found %s ad items on page %s.", len(list_items), current_page)
            except TimeoutError:
                LOG.warning("Could not find ad list container or items on page %s.", current_page)
                break  # Stop if ads disappear

            # Extract references using the CORRECTED selector
            try:
                page_refs = [
                    (await self.web_find(By.CSS_SELECTOR, "div.manageitems-item-ad h3 a.text-onSurface", parent = li)).attrs["href"]
                    for li in list_items
                ]
                refs.extend(page_refs)
                LOG.info("Successfully extracted %s refs from page %s.", len(page_refs), current_page)
            except Exception as e:
                # Log the error if extraction fails for some items, but try to continue
                LOG.exception("Error extracting refs on page %s: %s", current_page, e)

            if not multi_page:  # only one iteration for single-page overview
                break

            # --- Navigate to next page ---
            try:
                # Find the pagination section again (scope might have changed after scroll/wait)
                pagination_section = await self.web_find(By.CSS_SELECTOR, ".Pagination", timeout = 5)
                # Find the "Next" button using the correct aria-label selector and ensure it's not disabled
                next_button_element = None
                possible_next_buttons = await self.web_find_all(By.CSS_SELECTOR, 'button[aria-label="Nächste"]', parent = pagination_section)
                for btn in possible_next_buttons:
                    if not btn.attrs.get("disabled"):  # Check if the button is enabled
                        next_button_element = btn
                        break  # Found an enabled next button

                if next_button_element:
                    LOG.info("Navigating to next page...")
                    await next_button_element.click()
                    current_page += 1
                    # Wait for page load - consider waiting for a specific element on the new page instead of fixed sleep
                    await self.web_sleep(3000, 4000)
                else:
                    LOG.info('Last ad overview page explored (no enabled "Naechste" button found).')
                    break
            except TimeoutError:
                # This might happen if pagination disappears on the last page after loading
                LOG.info("No pagination controls found after scrolling/waiting. Assuming last page.")
                break
            except Exception as e:
                LOG.exception("Error during pagination navigation: %s", e)
                break
            # --- End Navigation ---

        if not refs:
            LOG.warning("No ad URLs were extracted.")

        return refs

    async def navigate_to_ad_page(self, id_or_url:int | str) -> bool:
        """
        Navigates to an ad page specified with an ad ID; or alternatively by a given URL.
        :return: whether the navigation to the ad page was successful
        """
        if reflect.is_integer(id_or_url):
            # navigate to search page
            await self.web_open("https://www.kleinanzeigen.de/s-suchanfrage.html?keywords={0}".format(id_or_url))
        else:
            await self.web_open(str(id_or_url))  # navigate to URL directly given
        await self.web_sleep()

        # handle the case that invalid ad ID given
        if self.page.url.endswith("k0"):
            LOG.error("There is no ad under the given ID.")
            return False

        # close (warning) popup, if given
        try:
            await self.web_find(By.ID, "vap-ovrly-secure")
            LOG.warning("A popup appeared!")
            await self.web_click(By.CLASS_NAME, "mfp-close")
            await self.web_sleep()
        except TimeoutError:
            pass
        return True

    async def _extract_ad_page_info(self, directory:str, ad_id:int) -> AdPartial:
        """
        Extracts all necessary information from an ad´s page.

        :param directory: the path of the ad´s previously created directory
        :param ad_id: the ad ID, already extracted by a calling function
        """
        info:dict[str, Any] = {"active": True}

        # extract basic info
        info["type"] = "OFFER" if "s-anzeige" in self.page.url else "WANTED"
        title:str = await self.web_text(By.ID, "viewad-title")
        LOG.info('Extracting information from ad with title "%s"', title)

        # contains info about ad in different dimensions in form of a key:value dict
        # dimension108 contains special attributes
        # dimension92 contains fake category, which becomes an special attribute on ad page
        belen_conf = await self.web_execute("window.BelenConf")

        info["category"] = await self._extract_category_from_ad_page()

        # append subcategory and change e.g. category "161/172" to "161/172/lautsprecher_kopfhoerer"
        # take subcategory from dimension92 as key 'art_s' sometimes is a special attribute (e.g. gender for clothes)
        # the subcategory isn't really necessary, but when set, the appropriate special attribute gets preselected
        if dimension92 := belen_conf["universalAnalyticsOpts"]["dimensions"].get("dimension92"):
            info["category"] += f"/{dimension92}"

        info["title"] = title

        # Get raw description text
        raw_description = (await self.web_text(By.ID, "viewad-description-text")).strip()

        # Get prefix and suffix from config
        prefix = self.config.ad_defaults.description_prefix
        suffix = self.config.ad_defaults.description_suffix

        # Remove prefix and suffix if present
        description_text = raw_description
        if prefix and description_text.startswith(prefix.strip()):
            description_text = description_text[len(prefix.strip()):]
        if suffix and description_text.endswith(suffix.strip()):
            description_text = description_text[:-len(suffix.strip())]

        info["description"] = description_text.strip()

        info["special_attributes"] = await self._extract_special_attributes_from_ad_page(belen_conf)

        if "schaden_s" in info["special_attributes"]:
            # change f to  'nein' and 't' to 'ja'
            info["special_attributes"]["schaden_s"] = info["special_attributes"]["schaden_s"].translate(str.maketrans({"t": "ja", "f": "nein"}))
        info["price"], info["price_type"] = await self._extract_pricing_info_from_ad_page()
        info["shipping_type"], info["shipping_costs"], info["shipping_options"] = await self._extract_shipping_info_from_ad_page()
        info["sell_directly"] = await self._extract_sell_directly_from_ad_page()
        info["images"] = await self._download_images_from_ad_page(directory, ad_id)
        info["contact"] = await self._extract_contact_from_ad_page()
        info["id"] = ad_id

        try:  # try different locations known for creation date element
            creation_date = await self.web_text(By.XPATH,
                "/html/body/div[1]/div[2]/div/section[2]/section/section/article/div[3]/div[2]/div[2]/div[1]/span")
        except TimeoutError:
            creation_date = await self.web_text(By.CSS_SELECTOR, "#viewad-extra-info > div:nth-child(1) > span:nth-child(2)")

        # convert creation date to ISO format
        created_parts = creation_date.split(".")
        creation_date = created_parts[2] + "-" + created_parts[1] + "-" + created_parts[0] + " 00:00:00"
        creation_date = datetime.fromisoformat(creation_date).isoformat()
        info["created_on"] = creation_date
        info["updated_on"] = None  # will be set later on

        ad_cfg = AdPartial.model_validate(info)

        # calculate the initial hash for the downloaded ad
        ad_cfg.content_hash = ad_cfg.to_ad(self.config.ad_defaults).update_content_hash().content_hash

        return ad_cfg

    async def _extract_category_from_ad_page(self) -> str:
        """
        Extracts a category of an ad in numerical form.
        Assumes that the web driver currently shows an ad page.

        :return: a category string of form abc/def, where a-f are digits
        """
        category_line = await self.web_find(By.ID, "vap-brdcrmb")
        category_first_part = await self.web_find(By.CSS_SELECTOR, "a:nth-of-type(2)", parent = category_line)
        category_second_part = await self.web_find(By.CSS_SELECTOR, "a:nth-of-type(3)", parent = category_line)
        cat_num_first = category_first_part.attrs["href"].rsplit("/", maxsplit = 1)[-1][1:]
        cat_num_second = category_second_part.attrs["href"].rsplit("/", maxsplit = 1)[-1][1:]
        category:str = cat_num_first + "/" + cat_num_second

        return category

    async def _extract_special_attributes_from_ad_page(self, belen_conf:dict[str, Any]) -> dict[str, Any]:
        """
        Extracts the special attributes from an ad page.
        If no items are available then special_attributes is empty

        :return: a dictionary (possibly empty) where the keys are the attribute names, mapped to their values
        """

        # e.g. "art_s:lautsprecher_kopfhoerer|condition_s:like_new|versand_s:t"
        special_attributes_str = belen_conf["universalAnalyticsOpts"]["dimensions"]["dimension108"]
        special_attributes = dict(item.split(":") for item in special_attributes_str.split("|") if ":" in item)
        special_attributes = {k: v for k, v in special_attributes.items() if not k.endswith(".versand_s") and k != "versand_s"}
        return special_attributes

    async def _extract_pricing_info_from_ad_page(self) -> tuple[float | None, str]:
        """
        Extracts the pricing information (price and pricing type) from an ad page.

        :return: the price of the offer (optional); and the pricing type
        """
        try:
            price_str:str = await self.web_text(By.ID, "viewad-price")
            price:int | None = None
            match price_str.rsplit(maxsplit = 1)[-1]:
                case "€":
                    price_type = "FIXED"
                    # replace('.', '') is to remove the thousands separator before parsing as int
                    price = int(price_str.replace(".", "").split(maxsplit = 1)[0])
                case "VB":
                    price_type = "NEGOTIABLE"
                    if price_str != "VB":  # can be either 'X € VB', or just 'VB'
                        price = int(price_str.replace(".", "").split(maxsplit = 1)[0])
                case "verschenken":
                    price_type = "GIVE_AWAY"
                case _:
                    price_type = "NOT_APPLICABLE"
            return price, price_type
        except TimeoutError:  # no 'commercial' ad, has no pricing box etc.
            return None, "NOT_APPLICABLE"

    async def _extract_shipping_info_from_ad_page(self) -> tuple[str, float | None, list[str] | None]:
        """
        Extracts shipping information from an ad page.

        :return: the shipping type, and the shipping price (optional)
        """
        ship_type, ship_costs, shipping_options = "NOT_APPLICABLE", None, None
        try:
            shipping_text = await self.web_text(By.CLASS_NAME, "boxedarticle--details--shipping")
            # e.g. '+ Versand ab 5,49 €' OR 'Nur Abholung'
            if shipping_text == "Nur Abholung":
                ship_type = "PICKUP"
            elif shipping_text == "Versand möglich":
                ship_type = "SHIPPING"
            elif "€" in shipping_text:
                shipping_price_parts = shipping_text.split(" ")
                ship_type = "SHIPPING"
                ship_costs = float(misc.parse_decimal(shipping_price_parts[-2]))

                # reading shipping option from kleinanzeigen
                # and find the right one by price
                shipping_costs = json.loads(
                    (await self.web_request("https://gateway.kleinanzeigen.de/postad/api/v1/shipping-options?posterType=PRIVATE"))
                    ["content"])["data"]["shippingOptionsResponse"]["options"]

                # map to internal shipping identifiers used by kleinanzeigen-bot
                shipping_option_mapping = {
                    "DHL_001": "DHL_2",
                    "DHL_002": "DHL_5",
                    "DHL_003": "DHL_10",
                    "DHL_004": "DHL_31,5",
                    "DHL_005": "DHL_20",
                    "HERMES_001": "Hermes_Päckchen",
                    "HERMES_002": "Hermes_S",
                    "HERMES_003": "Hermes_M",
                    "HERMES_004": "Hermes_L"
                }

                # Convert Euro to cents and round to nearest integer
                price_in_cent = round(ship_costs * 100)

                # If include_all_matching_shipping_options is enabled, get all options for the same package size
                if self.config.download.include_all_matching_shipping_options:
                    # Find all options with the same price to determine the package size
                    matching_options = [opt for opt in shipping_costs if opt["priceInEuroCent"] == price_in_cent]
                    if not matching_options:
                        return "NOT_APPLICABLE", ship_costs, shipping_options

                    # Use the package size of the first matching option
                    matching_size = matching_options[0]["packageSize"]

                    # Get all options of the same size
                    shipping_options = [
                        shipping_option_mapping[opt["id"]]
                        for opt in shipping_costs
                        if opt["packageSize"] == matching_size
                        and opt["id"] in shipping_option_mapping
                        and shipping_option_mapping[opt["id"]] not in self.config.download.excluded_shipping_options
                    ]
                else:
                    # Only use the matching option if it's not excluded
                    matching_option = next((x for x in shipping_costs if x["priceInEuroCent"] == price_in_cent), None)
                    if not matching_option:
                        return "NOT_APPLICABLE", ship_costs, shipping_options

                    shipping_option = shipping_option_mapping.get(matching_option["id"])
                    if not shipping_option or shipping_option in self.config.download.excluded_shipping_options:
                        return "NOT_APPLICABLE", ship_costs, shipping_options
                    shipping_options = [shipping_option]

        except TimeoutError:  # no pricing box -> no shipping given
            ship_type = "NOT_APPLICABLE"

        return ship_type, ship_costs, shipping_options

    async def _extract_sell_directly_from_ad_page(self) -> bool | None:
        """
        Extracts the sell directly option from an ad page.

        :return: a boolean indicating whether the sell directly option is active (optional)
        """
        try:
            buy_now_is_active:bool = "Direkt kaufen" in (await self.web_text(By.ID, "payment-buttons-sidebar"))
            return buy_now_is_active
        except TimeoutError:
            return None

    async def _extract_contact_from_ad_page(self) -> ContactPartial:
        """
        Processes the address part involving street (optional), zip code + city, and phone number (optional).

        :return: a dictionary containing the address parts with their corresponding values
        """
        contact:dict[str, (str | None)] = {}
        address_text = await self.web_text(By.ID, "viewad-locality")
        # format: e.g. (Beispiel Allee 42,) 12345 Bundesland - Stadt
        try:
            street = (await self.web_text(By.ID, "street-address"))[:-1]  # trailing comma
            contact["street"] = street
        except TimeoutError:
            LOG.info("No street given in the contact.")

        (zipcode, location) = address_text.split(" ", maxsplit = 1)
        contact["zipcode"] = zipcode  # e.g. 19372
        contact["location"] = location  # e.g. Mecklenburg-Vorpommern - Steinbeck

        contact_person_element:Element = await self.web_find(By.ID, "viewad-contact")
        name_element = await self.web_find(By.CLASS_NAME, "iconlist-text", parent = contact_person_element)
        try:
            name = await self.web_text(By.TAG_NAME, "a", parent = name_element)
        except TimeoutError:  # edge case: name without link
            name = await self.web_text(By.TAG_NAME, "span", parent = name_element)
        contact["name"] = name

        if "street" not in contact:
            contact["street"] = None
        try:  # phone number is unusual for non-professional sellers today
            phone_element = await self.web_find(By.ID, "viewad-contact-phone")
            phone_number = await self.web_text(By.TAG_NAME, "a", parent = phone_element)
            contact["phone"] = "".join(phone_number.replace("-", " ").split(" ")).replace("+49(0)", "0")
        except TimeoutError:
            contact["phone"] = None  # phone seems to be a deprecated feature (for non-professional users)
        # also see 'https://themen.kleinanzeigen.de/hilfe/deine-anzeigen/Telefon/

        return ContactPartial.model_validate(contact)
