"""初始化 ``asn_registry`` 名单（L1 初筛）的幂等种子脚本。

内置一份“精选首发集”：~70 条机房/云 ASN（category=cloud）与 ~110 条主流住宅 ISP
ASN（category=residential）。住宅 ASN 全球有上万条，这里是常见首发集，之后可在
网页「ASN 管理」里增删改。

用法：
    PYTHONPATH=. python scripts/seed_asn_registry.py [APP_DB_PATH]

未传路径时使用环境变量 / .env 里的 APP_DB_PATH。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if len(sys.argv) > 1:
    os.environ["APP_DB_PATH"] = sys.argv[1]

from app import db  # noqa: E402
from app.asn_store import normalize_asn  # noqa: E402

# (ASN, 机构, 国家) —— 机房 / 云厂商 / IDC
CLOUD = [
    ("16509", "Amazon AWS", "US"),
    ("14618", "Amazon AWS", "US"),
    ("8987", "Amazon", "US"),
    ("38895", "Amazon", "US"),
    ("15169", "Google", "US"),
    ("396982", "Google Cloud", "US"),
    ("19527", "Google", "US"),
    ("8075", "Microsoft Azure", "US"),
    ("12076", "Microsoft", "US"),
    ("14061", "DigitalOcean", "US"),
    ("16276", "OVH", "FR"),
    ("35540", "OVH", "FR"),
    ("24940", "Hetzner", "DE"),
    ("213230", "Hetzner", "DE"),
    ("20473", "Vultr (Choopa)", "US"),
    ("63949", "Linode / Akamai", "US"),
    ("20940", "Akamai", "US"),
    ("16625", "Akamai", "US"),
    ("31898", "Oracle Cloud", "US"),
    ("45102", "Alibaba Cloud", "CN"),
    ("37963", "Alibaba", "CN"),
    ("132203", "Tencent Cloud", "CN"),
    ("45090", "Tencent", "CN"),
    ("13335", "Cloudflare", "US"),
    ("209242", "Cloudflare", "US"),
    ("36351", "IBM SoftLayer", "US"),
    ("19994", "Rackspace", "US"),
    ("33070", "Rackspace", "US"),
    ("51167", "Contabo", "DE"),
    ("12876", "Scaleway / Online SAS", "FR"),
    ("60781", "Leaseweb", "NL"),
    ("28753", "Leaseweb", "DE"),
    ("30633", "Leaseweb", "US"),
    ("9009", "M247", "GB"),
    ("36352", "ColoCrossing", "US"),
    ("40676", "Psychz Networks", "US"),
    ("29761", "QuadraNet", "US"),
    ("53667", "FranTech / BuyVM", "US"),
    ("212238", "Datacamp / CDN77", "GB"),
    ("60068", "Datacamp", "GB"),
    ("62240", "Clouvider", "GB"),
    ("8560", "IONOS / 1&1", "DE"),
    ("6724", "Strato", "DE"),
    ("8972", "PlusServer", "DE"),
    ("49505", "Selectel", "RU"),
    ("51396", "Pfcloud", "RU"),
    ("202053", "UpCloud", "FI"),
    ("29802", "Hivelocity", "US"),
    ("46562", "Total Server Solutions", "US"),
    ("21859", "Zenlayer", "US"),
    ("55990", "Huawei Cloud", "CN"),
    ("136907", "Huawei Cloud", "CN"),
    ("44477", "Stark Industries", "DE"),
    ("210644", "Aeza", "RU"),
    ("135377", "UCloud", "CN"),
    ("26496", "GoDaddy", "US"),
    ("22612", "Namecheap", "US"),
    ("29066", "Velia.net", "DE"),
    ("49981", "WorldStream", "NL"),
    ("200651", "Flokinet", "RO"),
    ("34549", "meerfarbig", "DE"),
    ("54825", "Equinix Metal / Packet", "US"),
    ("15830", "Equinix", "US"),
    ("63023", "GTHost", "US"),
    ("18978", "Enzu", "US"),
    ("30083", "Server4You", "US"),
    ("32475", "SingleHop", "US"),
    ("46844", "Sharktech", "US"),
    ("33387", "DataShack / Nocix", "US"),
    ("22611", "InMotion Hosting", "US"),
    ("32780", "Hosting Services Inc", "US"),
]

# (ASN, 机构, 国家) —— 住宅 ISP / 电信运营商
RESIDENTIAL = [
    # 美国
    ("7922", "Comcast", "US"),
    ("7018", "AT&T", "US"),
    ("20057", "AT&T Mobility", "US"),
    ("701", "Verizon", "US"),
    ("22394", "Verizon Wireless", "US"),
    ("20115", "Charter Spectrum", "US"),
    ("11426", "Charter Spectrum", "US"),
    ("11427", "Charter Spectrum", "US"),
    ("33363", "Charter Spectrum", "US"),
    ("10796", "Charter Spectrum", "US"),
    ("22773", "Cox Communications", "US"),
    ("209", "Lumen / CenturyLink", "US"),
    ("5650", "Frontier", "US"),
    ("6128", "Optimum", "US"),
    ("7029", "Windstream", "US"),
    ("21928", "T-Mobile US", "US"),
    # 英国
    ("2856", "BT", "GB"),
    ("5089", "Virgin Media", "GB"),
    ("5607", "Sky Broadband", "GB"),
    ("13285", "TalkTalk", "GB"),
    ("5378", "Vodafone UK", "GB"),
    ("12576", "EE", "GB"),
    # 德国
    ("3320", "Deutsche Telekom", "DE"),
    ("3209", "Vodafone Deutschland", "DE"),
    ("31334", "Vodafone Kabel Deutschland", "DE"),
    ("6805", "Telefonica O2 Germany", "DE"),
    ("8881", "1&1 Versatel", "DE"),
    ("8422", "NetCologne", "DE"),
    # 法国
    ("3215", "Orange", "FR"),
    ("12322", "Free SAS", "FR"),
    ("15557", "SFR", "FR"),
    ("5410", "Bouygues Telecom", "FR"),
    # 意大利
    ("3269", "Telecom Italia", "IT"),
    ("12874", "Fastweb", "IT"),
    ("30722", "Vodafone Italia", "IT"),
    ("1267", "Wind Tre", "IT"),
    # 西班牙
    ("3352", "Telefonica", "ES"),
    ("12479", "Orange Espana", "ES"),
    ("12430", "Vodafone Spain", "ES"),
    # 荷兰
    ("1136", "KPN", "NL"),
    ("286", "KPN", "NL"),
    ("33915", "VodafoneZiggo", "NL"),
    ("13127", "Ziggo", "NL"),
    # 俄罗斯
    ("12389", "Rostelecom", "RU"),
    ("8359", "MTS", "RU"),
    ("3216", "Beeline", "RU"),
    ("8402", "Beeline", "RU"),
    ("31133", "Megafon", "RU"),
    ("20632", "Megafon", "RU"),
    ("25513", "MGTS", "RU"),
    # 中国
    ("4134", "China Telecom", "CN"),
    ("4809", "China Telecom CN2", "CN"),
    ("4837", "China Unicom", "CN"),
    ("9929", "China Unicom", "CN"),
    ("9808", "China Mobile", "CN"),
    ("56040", "China Mobile", "CN"),
    ("56048", "China Mobile", "CN"),
    # 日本
    ("4713", "NTT Communications", "JP"),
    ("2914", "NTT", "JP"),
    ("17676", "SoftBank", "JP"),
    ("2516", "KDDI", "JP"),
    ("2497", "IIJ", "JP"),
    # 韩国
    ("4766", "Korea Telecom", "KR"),
    ("9318", "SK Broadband", "KR"),
    ("3786", "LG U+", "KR"),
    # 新加坡
    ("9506", "SingTel", "SG"),
    ("4657", "StarHub", "SG"),
    # 澳大利亚
    ("1221", "Telstra", "AU"),
    ("4804", "Optus", "AU"),
    ("7545", "TPG", "AU"),
    ("9443", "Vocus", "AU"),
    # 加拿大
    ("852", "Bell Canada", "CA"),
    ("577", "Bell Canada", "CA"),
    ("6327", "Shaw Communications", "CA"),
    ("812", "Rogers", "CA"),
    # 巴西
    ("27699", "Vivo", "BR"),
    ("18881", "Vivo", "BR"),
    ("28573", "Claro", "BR"),
    ("7738", "Oi", "BR"),
    # 印度
    ("9498", "Bharti Airtel", "IN"),
    ("24560", "Bharti Airtel", "IN"),
    ("55836", "Reliance Jio", "IN"),
    ("9829", "BSNL", "IN"),
    # 墨西哥
    ("8151", "Telmex / Movistar", "MX"),
    # 中东
    ("5384", "Etisalat", "AE"),
    ("39891", "STC", "SA"),
    ("16135", "Turkcell", "TR"),
    ("9121", "Turk Telekom", "TR"),
    # 欧洲其他
    ("3303", "Swisscom", "CH"),
    ("6730", "Sunrise", "CH"),
    ("6848", "Telenet", "BE"),
    ("5432", "Proximus", "BE"),
    ("3301", "Telia", "SE"),
    ("2119", "Telenor", "NO"),
    ("3292", "TDC", "DK"),
    ("39651", "Tele2 / Com Hem", "SE"),
    ("8473", "Bahnhof", "SE"),
    ("719", "Elisa", "FI"),
    ("1759", "Telia Finland", "FI"),
    # 印尼 / 新西兰
    ("4761", "Indosat", "ID"),
    ("23693", "Telkomsel", "ID"),
    ("7713", "Telkom Indonesia", "ID"),
    ("4771", "Spark NZ", "NZ"),
]


def seed() -> None:
    db.init_app_db()
    now = int(time.time())
    n = 0
    for category, rows in (("cloud", CLOUD), ("residential", RESIDENTIAL)):
        for asn, org, country in rows:
            normalized = normalize_asn(asn)
            db.upsert_asn({
                "asn": normalized,
                "category": category,
                "org": org,
                "country": country,
                "source": "seed",
                "note": None,
                "enabled": 1,
                "created_at": now,
                "updated_at": now,
            })
            n += 1
    print(f"已写入/更新 ASN 名单: {n} 条 (cloud={len(CLOUD)}, residential={len(RESIDENTIAL)})")
    print(f"  应用库: {db.settings.app_db_path}")


if __name__ == "__main__":
    seed()
