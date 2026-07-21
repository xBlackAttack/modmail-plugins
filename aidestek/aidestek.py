# -*- coding: utf-8 -*-
"""
AI Destek — Modmail (modmail-dev/Modmail) için kendini geliştiren yapay zeka destek eklentisi.

Özellikler:
  * Ticket'a gelen kullanıcı mesajlarını Google Gemini ile analiz eder.
  * Bilgi bankasındaki (öğrenilmiş) kayıtlar veya Modmail snippet'leriyle eşleşen
    soruları ANINDA otomatik yanıtlar.
  * Yetkili bir soruya cevap verdiğinde soru-cevap çiftini bilgi bankasına
    kaydeder → sistem zamanla kendini geliştirir.
  * Her kullanıcı mesajında konuşmayı özetler; acil durum tespit ederse
    belirlenen rolleri etiketleyerek "Yetkiliye Aktarıldı" embed'i gönderir.

Kurulum için README.md dosyasına bakın.
"""

import asyncio
import copy
import json
from datetime import datetime, timezone

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel, getLogger

try:
    from google import genai
except ImportError:
    genai = None

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# API ANAHTARI (isteğe bağlı)
# .env dosyasına erişiminiz yoksa Google Gemini API anahtarınızı doğrudan
# aşağıya tırnakların arasına yapıştırın. Anahtar https://aistudio.google.com
# adresinden ücretsiz alınır. Örnek:
#   API_ANAHTARI = "AIzaSyXxxxxxxxxxxxxxxxxxxx"
# DİKKAT: Bu dosyayı anahtar yazılı hâldeyken GitHub'a veya başkasıyla
# paylaşmayın — anahtarınız ele geçirilebilir.
# ---------------------------------------------------------------------------
API_ANAHTARI = ""

MODEL_VARSAYILAN = "gemini-2.5-flash"

VARSAYILAN_AYAR = {
    "_id": "config",
    "rol_idler": [],            # acil durumda etiketlenecek rol ID'leri
    "guven_esigi": 0.80,        # otomatik cevap için minimum güven (0-1)
    "otomatik_cevap": True,     # eşleşen sorulara otomatik cevap gönder
    "otomatik_ogren": True,     # yetkili cevaplarından otomatik öğren
    "acil_bildirim": True,      # acil durum embed'i + rol etiketi
    "model": MODEL_VARSAYILAN,
    "api_key": None,            # boşsa API_ANAHTARI veya GEMINI_API_KEY kullanılır
    "sessiz_kanallar": [],      # AI'nin susturulduğu ticket kanalları
    "bildirim_kanal_id": None,  # acil bildirimlerin kopyalanacağı kanal (opsiyonel)
}

# ---------------------------------------------------------------------------
# Yapay zekaya verilecek talimatlar ve yapılandırılmış çıktı şemaları
# ---------------------------------------------------------------------------

TRIAJ_TALIMAT = """\
Sen bir Discord sunucusunun Modmail (ticket) destek sisteminde çalışan yapay zeka asistanısın.
Sana bir ticket'taki konuşma geçmişi ve kullanıcının yeni mesajı verilecek.
Ayrıca sistem tarafından bir "bilgi bankası" (daha önce öğrenilmiş soru-cevap kayıtları) ve
"snippet" listesi (hazır cevap şablonları) verilecek.

Görevlerin:
1. Kullanıcının yeni mesajını tek cümleyle özetle (alan: ozet).
2. Bilgi bankası veya snippet'lerden biri kullanıcının sorusunu GERÇEKTEN ve DOĞRUDAN
   yanıtlıyorsa eşleştir (alan: eslesme, biçim "kb:<id>" veya "snippet:<isim>"; yoksa null).
   - Sadece konu benzerliği yeterli DEĞİLDİR; kayıttaki cevap bu soruya gönderildiğinde
     kullanıcı tatmin olacaksa eşleştir.
   - Kullanıcıya özel durum gerektiren sorularda (hesaba özel işlem, ceza itirazı,
     kişisel inceleme gerektiren şikâyet) eşleştirme yapma.
3. Eşleşme güvenini 0 ile 1 arasında değerlendir (alan: guven). Emin değilsen düşük ver.
4. Aciliyet değerlendir (alan: acil). SADECE gerçek yetkili müdahalesi gerektiren durumlarda
   true ver: hesap güvenliği ihlali, dolandırıcılık, tehdit/taciz, kendine veya başkasına
   zarar, ödeme/para kaybı, sunucuyu etkileyen ciddi teknik arıza, yetkili istismarı,
   yasal konular. Sıradan sorular, başvuru soruları, genel merak acil DEĞİLDİR.
5. Her durumda konuşmanın mevcut durumunu eskalasyon alanlarına doldur:
   - musteri_talebi: kullanıcı ne istiyor
   - denenenler: şu ana kadar ne denendi ("Henüz belirlenmedi" yazabilirsin)
   - dogrulanan_sorun: doğrulanmış sorun ne ("Henüz belirlenmedi" yazabilirsin)
   - mevcut_engel: kullanıcıyı engelleyen şey ne
   - yetkili_dikkatine: yetkilinin yapması gereken şey ne

Tüm metin alanlarını TÜRKÇE yaz. Kısa ve net ol.
"""

TRIAJ_SEMA = {
    "type": "object",
    "properties": {
        "ozet": {"type": "string"},
        "eslesme": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "guven": {"type": "number"},
        "acil": {"type": "boolean"},
        "aciliyet_nedeni": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "eskalasyon": {
            "type": "object",
            "properties": {
                "musteri_talebi": {"type": "string"},
                "denenenler": {"type": "string"},
                "dogrulanan_sorun": {"type": "string"},
                "mevcut_engel": {"type": "string"},
                "yetkili_dikkatine": {"type": "string"},
            },
            "required": [
                "musteri_talebi", "denenenler", "dogrulanan_sorun",
                "mevcut_engel", "yetkili_dikkatine",
            ],
            "additionalProperties": False,
        },
    },
    "required": ["ozet", "eslesme", "guven", "acil", "aciliyet_nedeni", "eskalasyon"],
    "additionalProperties": False,
}

OGRENME_TALIMAT = """\
Sen bir Discord Modmail destek sisteminin öğrenme modülüsün.
Bir kullanıcı soru sordu ve bir yetkili cevap verdi. Görevin: bu soru-cevap çifti
gelecekte BAŞKA kullanıcılara da otomatik gönderilebilecek genel bir bilgi mi,
yoksa kişiye özel tek seferlik bir cevap mı, karar vermek.

Kurallar:
- Kişiye özel cevaplar (isim, hesaba özel işlem, tek seferlik karar, ceza detayı)
  ÖĞRENİLMEZ → ogrenilmeli=false.
- Selamlaşma, sohbet, "tamam", "rica ederim" gibi cevaplar ÖĞRENİLMEZ.
- Genel süreç/kural/bilgi cevapları ÖĞRENİLİR → ogrenilmeli=true.
- Öğrenilecekse:
  * baslik: 3-6 kelimelik kısa başlık
  * soru: sorunun genelleştirilmiş hâli (kişisel detaylar çıkarılmış)
  * cevap: cevabın genelleştirilmiş, her kullanıcıya gönderilebilir hâli
    (kişisel hitaplar ve kişiye özel detaylar çıkarılmış, anlam korunmuş)
- Sana mevcut bilgi bankası başlıkları da verilecek. Yeni bilgi mevcut bir kaydın
  daha iyi/güncel hâliyse guncelle_id alanına o kaydın id'sini yaz; tamamen yeniyse null bırak.

Tüm metinleri TÜRKÇE yaz.
"""

OGRENME_SEMA = {
    "type": "object",
    "properties": {
        "ogrenilmeli": {"type": "boolean"},
        "neden": {"type": "string"},
        "baslik": {"type": "string"},
        "soru": {"type": "string"},
        "cevap": {"type": "string"},
        "guncelle_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    },
    "required": ["ogrenilmeli", "neden", "baslik", "soru", "cevap", "guncelle_id"],
    "additionalProperties": False,
}

OZET_TALIMAT = """\
Sen bir Discord Modmail destek sisteminin özet modülüsün. Sana bir ticket'ın konuşma
geçmişi verilecek. Konuşmayı yetkili ekip için özetle. Tüm alanları TÜRKÇE doldur.
Bilinmeyen alanlara "Henüz belirlenmedi" yaz.
"""

OZET_SEMA = {
    "type": "object",
    "properties": {
        "genel_ozet": {"type": "string"},
        "musteri_talebi": {"type": "string"},
        "denenenler": {"type": "string"},
        "dogrulanan_sorun": {"type": "string"},
        "mevcut_engel": {"type": "string"},
        "yetkili_dikkatine": {"type": "string"},
    },
    "required": [
        "genel_ozet", "musteri_talebi", "denenenler",
        "dogrulanan_sorun", "mevcut_engel", "yetkili_dikkatine",
    ],
    "additionalProperties": False,
}


class AIDestek(commands.Cog):
    """Ticket'lar için kendini geliştiren yapay zeka destek asistanı."""

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.ayar = dict(VARSAYILAN_AYAR)
        self.client = None
        # kanal_id -> [(rol, icerik), ...] son konuşma geçmişi
        self.gecmis = {}
        # kanal_id -> {"soru": str, "zaman": datetime} — AI'nin cevaplayamadığı bekleyen soru
        self.bekleyen = {}
        # kanal_id -> asyncio.Lock — aynı ticket'ta eşzamanlı API çağrısını engelle
        self.kilitler = {}
        # işlenen son mesaj id'leri (çift tetiklenme koruması)
        self.islenen_mesajlar = set()

    # ------------------------------------------------------------------
    # Kurulum / yapılandırma
    # ------------------------------------------------------------------

    async def cog_load(self):
        await self._config_yukle()
        self._client_olustur()

    async def _config_yukle(self):
        doc = await self.db.find_one({"_id": "config"})
        if doc is None:
            await self.db.insert_one(dict(VARSAYILAN_AYAR))
            doc = dict(VARSAYILAN_AYAR)
        for k, v in VARSAYILAN_AYAR.items():
            doc.setdefault(k, v)
        self.ayar = doc

    async def _kaydet(self, **alanlar):
        self.ayar.update(alanlar)
        await self.db.update_one({"_id": "config"}, {"$set": alanlar}, upsert=True)

    def _client_olustur(self):
        if genai is None:
            logger.error("google-genai paketi kurulu değil; AI Destek devre dışı. (pip install google-genai)")
            self.client = None
            return
        import os
        anahtar = (
            self.ayar.get("api_key")
            or API_ANAHTARI.strip()
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not anahtar:
            logger.warning(
                "Gemini API anahtarı bulunamadı. `?ai anahtar <key>` komutuyla ayarlayın "
                "veya aidestek.py dosyasının başındaki API_ANAHTARI değişkenine yazın."
            )
            self.client = None
            return
        try:
            self.client = genai.Client(api_key=anahtar)
        except Exception:
            logger.error("Gemini istemcisi oluşturulamadı.", exc_info=True)
            self.client = None

    def _kilit(self, kanal_id):
        if kanal_id not in self.kilitler:
            self.kilitler[kanal_id] = asyncio.Lock()
        return self.kilitler[kanal_id]

    # ------------------------------------------------------------------
    # Bilgi bankası yardımcıları
    # ------------------------------------------------------------------

    async def _sonraki_id(self):
        doc = await self.db.find_one_and_update(
            {"_id": "sayac"}, {"$inc": {"deger": 1}}, upsert=True, return_document=True
        )
        return int(doc["deger"]) if doc and "deger" in doc else 1

    async def _bilgi_ekle(self, baslik, soru, cevap, kaynak, thread_id=None):
        kb_id = await self._sonraki_id()
        await self.db.insert_one({
            "tur": "bilgi",
            "kb_id": kb_id,
            "baslik": baslik,
            "soru": soru,
            "cevap": cevap,
            "kaynak": kaynak,
            "kullanim": 0,
            "olusturulma": datetime.now(timezone.utc),
            "thread_id": thread_id,
        })
        return kb_id

    async def _bilgileri_getir(self, limit=200):
        cursor = self.db.find({"tur": "bilgi"}).sort("kullanim", -1).limit(limit)
        return [b async for b in cursor]

    async def _istatistik(self, alan, artis=1):
        await self.db.update_one(
            {"_id": "istatistik"}, {"$inc": {alan: artis}}, upsert=True
        )

    def _bilgi_bankasi_metni(self, bilgiler):
        kayitlar = [
            {"id": b["kb_id"], "baslik": b["baslik"], "soru": b["soru"], "cevap": b["cevap"]}
            for b in sorted(bilgiler, key=lambda x: x["kb_id"])
        ]
        snippetler = [
            {"isim": isim, "icerik": str(icerik)[:500]}
            for isim, icerik in sorted(self.bot.snippets.items())
        ]
        return (
            "## Bilgi Bankası (JSON)\n"
            + json.dumps(kayitlar, ensure_ascii=False, sort_keys=True)
            + "\n\n## Snippetler (JSON)\n"
            + json.dumps(snippetler, ensure_ascii=False, sort_keys=True)
        )

    def _gecmis_metni(self, kanal_id):
        satirlar = self.gecmis.get(kanal_id, [])
        if not satirlar:
            return "(geçmiş yok)"
        return "\n".join(f"{rol}: {icerik}" for rol, icerik in satirlar[-20:])

    def _gecmise_ekle(self, kanal_id, rol, icerik):
        self.gecmis.setdefault(kanal_id, []).append((rol, str(icerik)[:1500]))
        # bellekte şişmesin
        if len(self.gecmis[kanal_id]) > 40:
            self.gecmis[kanal_id] = self.gecmis[kanal_id][-40:]

    # ------------------------------------------------------------------
    # Gemini çağrıları
    # ------------------------------------------------------------------

    @staticmethod
    def _json_ayikla(metin):
        """Model çıktısından JSON nesnesini güvenli biçimde çıkarır."""
        metin = (metin or "").strip()
        if metin.startswith("```"):
            metin = metin.strip("`")
            if metin.lower().startswith("json"):
                metin = metin[4:]
        ilk = metin.find("{")
        son = metin.rfind("}")
        if ilk == -1 or son == -1:
            raise ValueError("Çıktıda JSON bulunamadı")
        return json.loads(metin[ilk:son + 1])

    async def _ai_json(self, talimat, ek_sistem, kullanici_icerik, sema, max_tokens=2048):
        """Yapılandırılmış (JSON) çıktı ile tek Gemini çağrısı yapar."""
        if self.client is None:
            return None
        sistem = talimat
        if ek_sistem:
            sistem += "\n\n" + ek_sistem
        sistem += (
            "\n\n## ÇIKTI FORMATI (ZORUNLU)\n"
            "Yanıtını YALNIZCA aşağıdaki JSON şemasına birebir uyan geçerli bir JSON "
            "nesnesi olarak ver. JSON dışında hiçbir açıklama, metin veya kod bloğu yazma. "
            "Şemadaki tüm zorunlu alanları doldur.\n"
            + json.dumps(sema, ensure_ascii=False)
        )
        try:
            resp = await self.client.aio.models.generate_content(
                model=self.ayar.get("model", MODEL_VARSAYILAN),
                contents=kullanici_icerik,
                config={
                    "system_instruction": sistem,
                    "response_mime_type": "application/json",
                    "max_output_tokens": max_tokens,
                    "temperature": 0.2,
                },
            )
        except Exception:
            logger.error("Gemini API çağrısı başarısız oldu.", exc_info=True)
            return None
        try:
            return self._json_ayikla(resp.text)
        except Exception:
            logger.error("Gemini çıktısı çözümlenemedi: %r", getattr(resp, "text", None), exc_info=True)
            return None

    async def _triaj(self, kanal_id, yeni_mesaj):
        bilgiler = await self._bilgileri_getir()
        icerik = (
            "## Konuşma geçmişi\n"
            + self._gecmis_metni(kanal_id)
            + "\n\n## Kullanıcının yeni mesajı\n"
            + str(yeni_mesaj)[:3000]
        )
        sonuc = await self._ai_json(
            TRIAJ_TALIMAT, self._bilgi_bankasi_metni(bilgiler), icerik, TRIAJ_SEMA
        )
        return sonuc, bilgiler

    # ------------------------------------------------------------------
    # Otomatik cevap gönderme
    # ------------------------------------------------------------------

    async def _otomatik_cevapla(self, thread, orijinal_mesaj, cevap, kaynak_etiketi):
        """Cevabı Modmail'in resmi reply akışıyla (anonim) gönderir."""
        cevap = str(cevap)[:1900]
        try:
            sahte = copy.copy(orijinal_mesaj)
            sahte.content = cevap
            sahte.author = self.bot.modmail_guild.me
            sahte.attachments = []
            try:
                sahte.stickers = []
            except AttributeError:
                pass
            await thread.reply(sahte, anonymous=True)
        except Exception:
            logger.warning("thread.reply başarısız, doğrudan gönderime geçiliyor.", exc_info=True)
            embed = discord.Embed(description=cevap, color=self.bot.main_color)
            embed.set_author(name="Destek Asistanı")
            try:
                await thread.recipient.send(embed=embed)
            except Exception:
                logger.error("Kullanıcıya DM gönderilemedi.", exc_info=True)
                return False
            await thread.channel.send(embed=embed)

        bilgi = discord.Embed(
            description=f"🤖 **Otomatik yanıt gönderildi** — kaynak: `{kaynak_etiketi}`",
            color=discord.Color.green(),
        )
        bilgi.set_footer(text="Cevap yanlışsa: ?ai sil <id> | Bu ticket'ta sustur: ?ai sustur")
        try:
            await thread.channel.send(embed=bilgi)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # Acil durum / eskalasyon embed'i
    # ------------------------------------------------------------------

    def _eskalasyon_embed(self, thread, esk, neden=None, ozet=None):
        embed = discord.Embed(
            title="🚨 Yetkili Ekibe Aktarıldı",
            description=(
                "Yapay zeka bu talebin yetkili ekibe iletilmesi gerektiğini tespit etti.\n"
                "Lütfen bir yetkili en kısa sürede ilgilensin."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        alici = getattr(thread, "recipient", None)
        embed.add_field(name="Ticket", value=thread.channel.mention, inline=True)
        embed.add_field(name="Tür", value="Kullanıcı Desteği", inline=True)
        embed.add_field(
            name="Talep Sahibi",
            value=(alici.mention if alici else "Bilinmiyor"),
            inline=True,
        )
        if neden:
            embed.add_field(name="Aciliyet Nedeni", value=str(neden)[:1024], inline=False)
        if ozet:
            embed.add_field(name="Son Mesaj Özeti", value=str(ozet)[:1024], inline=False)
        embed.add_field(name="Müşteri Talebi", value=str(esk.get("musteri_talebi", "-"))[:1024], inline=False)
        embed.add_field(name="Denenenler", value=str(esk.get("denenenler", "-"))[:1024], inline=False)
        embed.add_field(name="Doğrulanan Sorun", value=str(esk.get("dogrulanan_sorun", "-"))[:1024], inline=False)
        embed.add_field(name="Mevcut Engel", value=str(esk.get("mevcut_engel", "-"))[:1024], inline=False)
        embed.add_field(name="Yetkili Dikkatine", value=str(esk.get("yetkili_dikkatine", "-"))[:1024], inline=False)
        embed.set_footer(text="AI Destek • otomatik eskalasyon")
        return embed

    async def _eskale_et(self, thread, esk, neden=None, ozet=None):
        roller = [f"<@&{rid}>" for rid in self.ayar.get("rol_idler", [])]
        icerik = " ".join(roller) if roller else None
        embed = self._eskalasyon_embed(thread, esk, neden=neden, ozet=ozet)
        izinler = discord.AllowedMentions(roles=True, users=False, everyone=False)
        try:
            await thread.channel.send(content=icerik, embed=embed, allowed_mentions=izinler)
        except Exception:
            logger.error("Eskalasyon embed'i gönderilemedi.", exc_info=True)

        # opsiyonel bildirim kanalı
        kanal_id = self.ayar.get("bildirim_kanal_id")
        if kanal_id:
            kanal = self.bot.get_channel(int(kanal_id))
            if kanal:
                try:
                    await kanal.send(content=icerik, embed=embed, allowed_mentions=izinler)
                except Exception:
                    pass
        await self._istatistik("eskalasyon")

    # ------------------------------------------------------------------
    # Ana akış: kullanıcı mesajı geldiğinde
    # ------------------------------------------------------------------

    async def _kullanici_mesaji_isle(self, thread, message):
        kanal = getattr(thread, "channel", None)
        if kanal is None or self.client is None:
            return
        if kanal.id in self.ayar.get("sessiz_kanallar", []):
            return
        icerik = (message.content or "").strip()
        if not icerik:
            return

        async with self._kilit(kanal.id):
            sonuc, bilgiler = await self._triaj(kanal.id, icerik)
            if sonuc is None:
                return

            esik = float(self.ayar.get("guven_esigi", 0.8))
            eslesme = sonuc.get("eslesme")
            guven = float(sonuc.get("guven") or 0)
            cevaplandi = False

            # 1) Otomatik cevap
            if self.ayar.get("otomatik_cevap") and eslesme and guven >= esik:
                cevap = None
                if eslesme.startswith("kb:"):
                    try:
                        kb_id = int(eslesme.split(":", 1)[1])
                    except ValueError:
                        kb_id = None
                    kayit = next((b for b in bilgiler if b["kb_id"] == kb_id), None)
                    if kayit:
                        cevap = kayit["cevap"]
                        await self.db.update_one(
                            {"tur": "bilgi", "kb_id": kb_id}, {"$inc": {"kullanim": 1}}
                        )
                        etiket = f"bilgi #{kb_id} ({kayit['baslik']}) • güven {guven:.2f}"
                elif eslesme.startswith("snippet:"):
                    isim = eslesme.split(":", 1)[1]
                    ham = self.bot.snippets.get(isim)
                    if ham:
                        cevap = str(ham)
                        etiket = f"snippet '{isim}' • güven {guven:.2f}"

                if cevap:
                    cevaplandi = await self._otomatik_cevapla(thread, message, cevap, etiket)
                    if cevaplandi:
                        self._gecmise_ekle(kanal.id, "AI (otomatik cevap)", cevap)
                        self.bekleyen.pop(kanal.id, None)
                        await self._istatistik("otomatik_cevap")

            # 2) Cevaplanamadıysa: yetkili cevabından öğrenmek üzere beklemeye al
            if not cevaplandi:
                self.bekleyen[kanal.id] = {
                    "soru": icerik,
                    "zaman": datetime.now(timezone.utc),
                }

            # 3) Acil durum kontrolü
            if self.ayar.get("acil_bildirim") and sonuc.get("acil"):
                await self._eskale_et(
                    thread,
                    sonuc.get("eskalasyon", {}),
                    neden=sonuc.get("aciliyet_nedeni"),
                    ozet=sonuc.get("ozet"),
                )

    # ------------------------------------------------------------------
    # Öğrenme akışı: yetkili cevap verdiğinde
    # ------------------------------------------------------------------

    async def _yetkili_cevabindan_ogren(self, thread, message):
        kanal = getattr(thread, "channel", None)
        if kanal is None or self.client is None:
            return
        bekleyen = self.bekleyen.get(kanal.id)
        if not bekleyen or not self.ayar.get("otomatik_ogren"):
            return
        cevap_icerik = (message.content or "").strip()
        if not cevap_icerik or len(cevap_icerik) < 10:
            return

        bilgiler = await self._bilgileri_getir()
        basliklar = json.dumps(
            [{"id": b["kb_id"], "baslik": b["baslik"], "soru": b["soru"]} for b in bilgiler],
            ensure_ascii=False,
            sort_keys=True,
        )
        icerik = (
            "## Kullanıcının sorusu\n" + bekleyen["soru"][:2000]
            + "\n\n## Yetkilinin cevabı\n" + cevap_icerik[:2000]
            + "\n\n## Mevcut bilgi bankası başlıkları (JSON)\n" + basliklar
        )
        sonuc = await self._ai_json(OGRENME_TALIMAT, None, icerik, OGRENME_SEMA)
        # Soru artık cevaplandı — beklemeden çıkar
        self.bekleyen.pop(kanal.id, None)
        if not sonuc or not sonuc.get("ogrenilmeli"):
            return

        guncelle_id = sonuc.get("guncelle_id")
        if guncelle_id is not None:
            var = await self.db.find_one({"tur": "bilgi", "kb_id": int(guncelle_id)})
            if var:
                await self.db.update_one(
                    {"tur": "bilgi", "kb_id": int(guncelle_id)},
                    {"$set": {
                        "baslik": sonuc["baslik"],
                        "soru": sonuc["soru"],
                        "cevap": sonuc["cevap"],
                        "guncellenme": datetime.now(timezone.utc),
                    }},
                )
                kb_id, eylem = int(guncelle_id), "güncellendi"
            else:
                kb_id = await self._bilgi_ekle(
                    sonuc["baslik"], sonuc["soru"], sonuc["cevap"], "ogrenildi", kanal.id
                )
                eylem = "öğrenildi"
        else:
            kb_id = await self._bilgi_ekle(
                sonuc["baslik"], sonuc["soru"], sonuc["cevap"], "ogrenildi", kanal.id
            )
            eylem = "öğrenildi"

        await self._istatistik("ogrenilen")
        embed = discord.Embed(
            description=(
                f"📚 **Yeni bilgi {eylem}:** `#{kb_id}` — {sonuc['baslik']}\n"
                f"Bu soru bir daha sorulduğunda otomatik cevaplanacak."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"İncele: ?ai bilgi {kb_id} | Yanlışsa: ?ai sil {kb_id}")
        try:
            await kanal.send(embed=embed)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Modmail olayları
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator=None, category=None, initial_message=None):
        """Yeni ticket açıldığında ilk mesajı işle."""
        if initial_message is None:
            return
        if initial_message.id in self.islenen_mesajlar:
            return
        self.islenen_mesajlar.add(initial_message.id)
        kanal = getattr(thread, "channel", None)
        if kanal is None:
            return
        self._gecmise_ekle(kanal.id, "Kullanıcı", initial_message.content or "")
        try:
            await self._kullanici_mesaji_isle(thread, initial_message)
        except Exception:
            logger.error("İlk mesaj işlenirken hata oluştu.", exc_info=True)

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        kanal = getattr(thread, "channel", None)
        if kanal is None:
            return
        if message.id in self.islenen_mesajlar:
            return
        self.islenen_mesajlar.add(message.id)
        if len(self.islenen_mesajlar) > 5000:
            self.islenen_mesajlar = set(list(self.islenen_mesajlar)[-2000:])

        try:
            if from_mod:
                # kendi otomatik cevabımızı tekrar işleme
                if message.author.id == self.bot.user.id:
                    return
                self._gecmise_ekle(kanal.id, "Yetkili", message.content or "")
                await self._yetkili_cevabindan_ogren(thread, message)
            else:
                self._gecmise_ekle(kanal.id, "Kullanıcı", message.content or "")
                await self._kullanici_mesaji_isle(thread, message)
        except Exception:
            logger.error("thread_reply işlenirken hata oluştu.", exc_info=True)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, *args, **kwargs):
        kanal = getattr(thread, "channel", None)
        if kanal is None:
            return
        self.gecmis.pop(kanal.id, None)
        self.bekleyen.pop(kanal.id, None)
        self.kilitler.pop(kanal.id, None)
        if kanal.id in self.ayar.get("sessiz_kanallar", []):
            yeni = [k for k in self.ayar["sessiz_kanallar"] if k != kanal.id]
            await self._kaydet(sessiz_kanallar=yeni)

    # ------------------------------------------------------------------
    # Komutlar
    # ------------------------------------------------------------------

    @commands.group(name="ai", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ai(self, ctx):
        """AI Destek komutları."""
        embed = discord.Embed(
            title="🤖 AI Destek Komutları",
            color=self.bot.main_color,
            description=(
                "`?ai durum` — ayarlar ve istatistikler\n"
                "`?ai ac` / `?ai kapat` — otomatik cevabı aç/kapat\n"
                "`?ai rolekle @rol` / `?ai rolsil @rol` — acil durum rolleri\n"
                "`?ai esik 0.8` — otomatik cevap güven eşiği\n"
                "`?ai ogrenme ac|kapat` — otomatik öğrenmeyi aç/kapat\n"
                "`?ai ogret \"soru\" \"cevap\"` — elle bilgi ekle\n"
                "`?ai bilgiler [sayfa]` — bilgi bankasını listele\n"
                "`?ai bilgi <id>` — kaydı görüntüle\n"
                "`?ai sil <id>` — kaydı sil\n"
                "`?ai test <soru>` — cevap göndermeden eşleşmeyi dene\n"
                "`?ai ozet` — bu ticket'ı özetle (ticket kanalında)\n"
                "`?ai eskale` — bu ticket'ı elle yetkiliye aktar\n"
                "`?ai sustur` — bu ticket'ta AI'yi sustur/aç\n"
                "`?ai bildirimkanal [#kanal]` — acil bildirim kopya kanalı\n"
                "`?ai model <model-id>` — kullanılan Gemini modeli\n"
                "`?ai anahtar <key>` — Gemini API anahtarı (mesaj silinir)"
            ),
        )
        await ctx.send(embed=embed)

    @ai.command(name="durum")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ai_durum(self, ctx):
        """Ayarları ve istatistikleri gösterir."""
        ist = await self.db.find_one({"_id": "istatistik"}) or {}
        bilgi_sayisi = await self.db.count_documents({"tur": "bilgi"})
        roller = ", ".join(f"<@&{r}>" for r in self.ayar.get("rol_idler", [])) or "—"
        embed = discord.Embed(title="🤖 AI Destek — Durum", color=self.bot.main_color)
        embed.add_field(name="API", value="✅ hazır" if self.client else "❌ anahtar yok", inline=True)
        embed.add_field(name="Model", value=f"`{self.ayar.get('model')}`", inline=True)
        embed.add_field(name="Güven Eşiği", value=f"{self.ayar.get('guven_esigi')}", inline=True)
        embed.add_field(name="Otomatik Cevap", value="✅" if self.ayar.get("otomatik_cevap") else "❌", inline=True)
        embed.add_field(name="Otomatik Öğrenme", value="✅" if self.ayar.get("otomatik_ogren") else "❌", inline=True)
        embed.add_field(name="Acil Bildirim", value="✅" if self.ayar.get("acil_bildirim") else "❌", inline=True)
        embed.add_field(name="Acil Durum Rolleri", value=roller, inline=False)
        embed.add_field(
            name="İstatistikler",
            value=(
                f"Bilgi bankası kaydı: **{bilgi_sayisi}**\n"
                f"Otomatik cevap: **{ist.get('otomatik_cevap', 0)}**\n"
                f"Öğrenilen bilgi: **{ist.get('ogrenilen', 0)}**\n"
                f"Eskalasyon: **{ist.get('eskalasyon', 0)}**"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @ai.command(name="ac")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_ac(self, ctx):
        """Otomatik cevabı açar."""
        await self._kaydet(otomatik_cevap=True)
        await ctx.send("✅ Otomatik cevap **açıldı**.")

    @ai.command(name="kapat")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_kapat(self, ctx):
        """Otomatik cevabı kapatır."""
        await self._kaydet(otomatik_cevap=False)
        await ctx.send("⛔ Otomatik cevap **kapatıldı**. (Öğrenme ve acil bildirim çalışmaya devam eder.)")

    @ai.command(name="rolekle")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_rolekle(self, ctx, rol: discord.Role):
        """Acil durumlarda etiketlenecek rol ekler."""
        roller = self.ayar.get("rol_idler", [])
        if rol.id in roller:
            return await ctx.send("Bu rol zaten ekli.")
        roller.append(rol.id)
        await self._kaydet(rol_idler=roller)
        await ctx.send(f"✅ Acil durum rolü eklendi: {rol.mention}")

    @ai.command(name="rolsil")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_rolsil(self, ctx, rol: discord.Role):
        """Acil durum rolünü kaldırır."""
        roller = [r for r in self.ayar.get("rol_idler", []) if r != rol.id]
        await self._kaydet(rol_idler=roller)
        await ctx.send(f"🗑️ Rol kaldırıldı: {rol.mention}")

    @ai.command(name="esik")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_esik(self, ctx, deger: float):
        """Otomatik cevap güven eşiğini ayarlar (0-1)."""
        if not 0 <= deger <= 1:
            return await ctx.send("Eşik 0 ile 1 arasında olmalı. Örnek: `?ai esik 0.8`")
        await self._kaydet(guven_esigi=deger)
        await ctx.send(f"✅ Güven eşiği **{deger}** olarak ayarlandı.")

    @ai.command(name="ogrenme")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_ogrenme(self, ctx, secim: str):
        """Otomatik öğrenmeyi açar/kapatır: ?ai ogrenme ac|kapat"""
        secim = secim.lower()
        if secim not in ("ac", "aç", "kapat"):
            return await ctx.send("Kullanım: `?ai ogrenme ac` veya `?ai ogrenme kapat`")
        acik = secim in ("ac", "aç")
        await self._kaydet(otomatik_ogren=acik)
        await ctx.send(f"{'✅ Otomatik öğrenme açıldı.' if acik else '⛔ Otomatik öğrenme kapatıldı.'}")

    @ai.command(name="ogret")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ai_ogret(self, ctx, soru: str, *, cevap: str):
        """Elle bilgi ekler: ?ai ogret "soru" cevap metni"""
        baslik = soru[:60]
        kb_id = await self._bilgi_ekle(baslik, soru, cevap, "manuel")
        await ctx.send(f"✅ Bilgi eklendi: `#{kb_id}` — {baslik}")

    @ai.command(name="bilgiler")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ai_bilgiler(self, ctx, sayfa: int = 1):
        """Bilgi bankasını listeler."""
        hepsi = await self._bilgileri_getir(limit=1000)
        hepsi.sort(key=lambda b: b["kb_id"])
        if not hepsi:
            return await ctx.send("Bilgi bankası boş. Sistem, yetkili cevaplarından otomatik öğrenecek.")
        sayfa_boyu = 10
        toplam_sayfa = (len(hepsi) + sayfa_boyu - 1) // sayfa_boyu
        sayfa = max(1, min(sayfa, toplam_sayfa))
        dilim = hepsi[(sayfa - 1) * sayfa_boyu: sayfa * sayfa_boyu]
        satirlar = [
            f"`#{b['kb_id']}` **{b['baslik']}** — {b['kaynak']}, {b['kullanim']} kez kullanıldı"
            for b in dilim
        ]
        embed = discord.Embed(
            title=f"📚 Bilgi Bankası ({len(hepsi)} kayıt)",
            description="\n".join(satirlar),
            color=self.bot.main_color,
        )
        embed.set_footer(text=f"Sayfa {sayfa}/{toplam_sayfa} • Detay: ?ai bilgi <id>")
        await ctx.send(embed=embed)

    @ai.command(name="bilgi")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ai_bilgi(self, ctx, kb_id: int):
        """Bir bilgi kaydının detayını gösterir."""
        b = await self.db.find_one({"tur": "bilgi", "kb_id": kb_id})
        if not b:
            return await ctx.send(f"`#{kb_id}` bulunamadı.")
        embed = discord.Embed(title=f"📖 Bilgi #{kb_id} — {b['baslik']}", color=self.bot.main_color)
        embed.add_field(name="Soru", value=str(b["soru"])[:1024], inline=False)
        embed.add_field(name="Cevap", value=str(b["cevap"])[:1024], inline=False)
        embed.add_field(name="Kaynak", value=b.get("kaynak", "-"), inline=True)
        embed.add_field(name="Kullanım", value=str(b.get("kullanim", 0)), inline=True)
        await ctx.send(embed=embed)

    @ai.command(name="sil")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ai_sil(self, ctx, kb_id: int):
        """Bilgi kaydını siler."""
        sonuc = await self.db.delete_one({"tur": "bilgi", "kb_id": kb_id})
        if sonuc.deleted_count:
            await ctx.send(f"🗑️ `#{kb_id}` silindi.")
        else:
            await ctx.send(f"`#{kb_id}` bulunamadı.")

    @ai.command(name="test")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ai_test(self, ctx, *, soru: str):
        """Cevap göndermeden bir sorunun nasıl işleneceğini gösterir."""
        if self.client is None:
            return await ctx.send("❌ API anahtarı ayarlı değil. `?ai anahtar <key>`")
        async with ctx.typing():
            sonuc, _ = await self._triaj(ctx.channel.id, soru)
        if sonuc is None:
            return await ctx.send("❌ API çağrısı başarısız oldu (loglara bakın).")
        embed = discord.Embed(title="🧪 Test Sonucu", color=self.bot.main_color)
        embed.add_field(name="Özet", value=str(sonuc.get("ozet", "-"))[:1024], inline=False)
        embed.add_field(name="Eşleşme", value=str(sonuc.get("eslesme")), inline=True)
        embed.add_field(name="Güven", value=f"{sonuc.get('guven', 0):.2f}", inline=True)
        embed.add_field(name="Acil mi?", value="🚨 Evet" if sonuc.get("acil") else "Hayır", inline=True)
        esik = self.ayar.get("guven_esigi", 0.8)
        gonderilir = bool(sonuc.get("eslesme")) and float(sonuc.get("guven") or 0) >= esik
        embed.add_field(
            name="Sonuç",
            value="✅ Otomatik cevap **gönderilirdi**." if gonderilir else "❌ Otomatik cevap gönderilmezdi.",
            inline=False,
        )
        await ctx.send(embed=embed)

    @ai.command(name="ozet")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_ozet(self, ctx):
        """Bulunduğunuz ticket'ı özetler."""
        thread = await self.bot.threads.find(channel=ctx.channel)
        if thread is None:
            return await ctx.send("Bu komut yalnızca bir ticket kanalında kullanılabilir.")
        if self.client is None:
            return await ctx.send("❌ API anahtarı ayarlı değil. `?ai anahtar <key>`")
        async with ctx.typing():
            icerik = "## Konuşma geçmişi\n" + self._gecmis_metni(ctx.channel.id)
            sonuc = await self._ai_json(OZET_TALIMAT, None, icerik, OZET_SEMA, max_tokens=4096)
        if sonuc is None:
            return await ctx.send("❌ Özet oluşturulamadı.")
        embed = discord.Embed(
            title="📋 Ticket Özeti",
            description=str(sonuc.get("genel_ozet", "-"))[:2000],
            color=self.bot.main_color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Müşteri Talebi", value=str(sonuc.get("musteri_talebi", "-"))[:1024], inline=False)
        embed.add_field(name="Denenenler", value=str(sonuc.get("denenenler", "-"))[:1024], inline=False)
        embed.add_field(name="Doğrulanan Sorun", value=str(sonuc.get("dogrulanan_sorun", "-"))[:1024], inline=False)
        embed.add_field(name="Mevcut Engel", value=str(sonuc.get("mevcut_engel", "-"))[:1024], inline=False)
        embed.add_field(name="Yetkili Dikkatine", value=str(sonuc.get("yetkili_dikkatine", "-"))[:1024], inline=False)
        await ctx.send(embed=embed)

    @ai.command(name="eskale")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_eskale(self, ctx):
        """Bulunduğunuz ticket'ı elle yetkiliye aktarır."""
        thread = await self.bot.threads.find(channel=ctx.channel)
        if thread is None:
            return await ctx.send("Bu komut yalnızca bir ticket kanalında kullanılabilir.")
        if self.client is None:
            return await ctx.send("❌ API anahtarı ayarlı değil. `?ai anahtar <key>`")
        async with ctx.typing():
            icerik = "## Konuşma geçmişi\n" + self._gecmis_metni(ctx.channel.id)
            sonuc = await self._ai_json(OZET_TALIMAT, None, icerik, OZET_SEMA, max_tokens=4096)
        esk = sonuc or {}
        await self._eskale_et(thread, esk, neden="Yetkili tarafından elle aktarıldı.")

    @ai.command(name="sustur")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ai_sustur(self, ctx):
        """Bu ticket'ta AI'yi susturur/açar."""
        sessizler = self.ayar.get("sessiz_kanallar", [])
        if ctx.channel.id in sessizler:
            sessizler = [k for k in sessizler if k != ctx.channel.id]
            await self._kaydet(sessiz_kanallar=sessizler)
            await ctx.send("🔊 AI bu ticket'ta tekrar **aktif**.")
        else:
            sessizler.append(ctx.channel.id)
            await self._kaydet(sessiz_kanallar=sessizler)
            await ctx.send("🔇 AI bu ticket'ta **susturuldu**. Tekrar açmak için aynı komutu kullanın.")

    @ai.command(name="bildirimkanal")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_bildirimkanal(self, ctx, kanal: discord.TextChannel = None):
        """Acil bildirimlerin kopyalanacağı kanalı ayarlar (boş bırakılırsa kaldırır)."""
        if kanal is None:
            await self._kaydet(bildirim_kanal_id=None)
            return await ctx.send("Bildirim kanalı kaldırıldı.")
        await self._kaydet(bildirim_kanal_id=kanal.id)
        await ctx.send(f"✅ Acil bildirimler ayrıca {kanal.mention} kanalına gönderilecek.")

    @ai.command(name="model")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_model(self, ctx, model_id: str):
        """Kullanılacak Gemini modelini ayarlar (ör. gemini-2.5-pro, gemini-2.5-flash)."""
        await self._kaydet(model=model_id)
        await ctx.send(f"✅ Model `{model_id}` olarak ayarlandı.")

    @ai.command(name="anahtar")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def ai_anahtar(self, ctx, *, anahtar: str):
        """Gemini API anahtarını ayarlar. Güvenlik için mesajınız silinir."""
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await self._kaydet(api_key=anahtar.strip())
        self._client_olustur()
        durum = "✅ API anahtarı kaydedildi ve bağlantı hazır." if self.client else "⚠️ Anahtar kaydedildi ama istemci oluşturulamadı (loglara bakın)."
        await ctx.send(durum)


async def setup(bot):
    await bot.add_cog(AIDestek(bot))
