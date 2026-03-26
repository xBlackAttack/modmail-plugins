"""
restricted_hours - Modmail Plugin
==================================
Bu plugin, belirli bir role sahip olmayan kullanıcıların
bota sadece belirli saatler arasında (15:00 - 18:00) mesaj atmasına izin verir.
Belirtilen role sahip olanlar her zaman ticket açabilir.

Kurulum:
  ?plugin add <github_kullanici_adi>/<repo_adi>/restricted_hours
  
Plugin dosyasını kendi GitHub reponuzda plugins/ klasörüne koyun.
"""

import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta


# ─── AYARLAR ────────────────────────────────────────────────────────────────

# Kısıtlamadan muaf olan role'nin ID'si
EXEMPT_ROLE_ID = 1407104983672950986

# İzin verilen saat aralığı (24 saat formatı, Türkiye saati UTC+3)
ALLOWED_START_HOUR = 15   # 15:00
ALLOWED_END_HOUR   = 18   # 18:00 (bu saat dahil DEĞİL, yani 17:59'a kadar)

# Türkiye saat dilimi (UTC+3)
TURKEY_TZ = timezone(timedelta(hours=3))

# Kullanıcıya gönderilecek mesajlar
MSG_OUTSIDE_HOURS = (
    "🕐 **Şu anda hizmet vermiyoruz.**\n\n"
    "Destek ekibimize **her gün saat 15:00 – 18:00** (Türkiye saati) arasında ulaşabilirsiniz.\n"
    "Bu saatler dışında gönderilen mesajlar işleme alınmaz.\n\n"
    "Anlayışınız için teşekkür ederiz. 🙏"
)

# ─────────────────────────────────────────────────────────────────────────────


class RestrictedHours(commands.Cog):
    """Belirli saatler dışında ticket açılmasını engelleyen plugin."""

    def __init__(self, bot):
        self.bot = bot

    def _is_within_allowed_hours(self) -> bool:
        """Şu anki saatin izin verilen aralıkta olup olmadığını kontrol eder."""
        now = datetime.now(TURKEY_TZ)
        return ALLOWED_START_HOUR <= now.hour < ALLOWED_END_HOUR

    def _user_has_exempt_role(self, member: discord.Member) -> bool:
        """Kullanıcının muaf rolüne sahip olup olmadığını kontrol eder."""
        if member is None:
            return False
        return any(role.id == EXEMPT_ROLE_ID for role in member.roles)

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """
        Yeni bir thread (ticket) oluşturulduğunda tetiklenir.
        Eğer kullanıcı muaf değilse ve saat uygun değilse thread kapatılır.
        """
        # Guild member nesnesini al
        guild = self.bot.guild
        try:
            member = guild.get_member(creator.id) or await guild.fetch_member(creator.id)
        except (discord.NotFound, discord.HTTPException):
            member = None

        # Muaf role kontrolü
        if self._user_has_exempt_role(member):
            return  # Kısıtlama yok, devam et

        # Saat kontrolü
        if self._is_within_allowed_hours():
            return  # Uygun saatte, devam et

        # ── Kısıtlama devreye giriyor ──

        # Kullanıcıya DM gönder
        try:
            await creator.send(MSG_OUTSIDE_HOURS)
        except discord.Forbidden:
            pass  # DM kapalıysa sessizce geç

        # Thread'i hemen kapat (kullanıcıya bilgi notu bırakarak)
        await thread.close(
            closer=self.bot.modmail_bot,
            silent=True,
            delete_channel=False,
            message=f"🔒 Otomatik kapatıldı: Kullanıcı mesajı çalışma saatleri ({ALLOWED_START_HOUR}:00-{ALLOWED_END_HOUR}:00) dışında gönderdi.",
            auto_close=False,
        )


async def setup(bot):
    await bot.add_cog(RestrictedHours(bot))
