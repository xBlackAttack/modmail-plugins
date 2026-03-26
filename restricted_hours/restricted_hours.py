import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta

# ─── AYARLAR ────────────────────────────────────────────────────────────────

EXEMPT_ROLE_ID = 1407104983672950986

ALLOWED_START_HOUR = 15   # 15:00
ALLOWED_END_HOUR   = 18   # 18:00

TURKEY_TZ = timezone(timedelta(hours=3))

MSG_OUTSIDE_HOURS = (
    "🕐 **Şu anda hizmet vermiyoruz.**\n\n"
    "Destek ekibimize **her gün saat 15:00 – 18:00** (Türkiye saati) arasında ulaşabilirsiniz.\n"
    "Bu saatler dışında gönderilen mesajlar işleme alınmaz.\n\n"
    "Anlayışınız için teşekkür ederiz. 🙏"
)

# ─────────────────────────────────────────────────────────────────────────────


class RestrictedHours(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_within_allowed_hours(self) -> bool:
        now = datetime.now(TURKEY_TZ)
        return ALLOWED_START_HOUR <= now.hour < ALLOWED_END_HOUR

    def _user_has_exempt_role(self, member: discord.Member) -> bool:
        if member is None:
            return False
        return any(role.id == EXEMPT_ROLE_ID for role in member.roles)

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        guild = self.bot.modmail_guild

        try:
            member = guild.get_member(creator.id) or await guild.fetch_member(creator.id)
        except (discord.NotFound, discord.HTTPException):
            member = None

        # Muaf role sahipse, serbest
        if self._user_has_exempt_role(member):
            return

        # Saat uygunsa, serbest
        if self._is_within_allowed_hours():
            return

        # ── Kısıtlama devreye giriyor ──

        # Kullanıcıya DM gönder
        try:
            await creator.send(MSG_OUTSIDE_HOURS)
        except discord.Forbidden:
            pass

        # Thread kanalına da mesaj yaz (log için)
        try:
            await thread.channel.send(
                f"🔒 **Otomatik kapatıldı:** Kullanıcı mesajı çalışma saatleri "
                f"dışında ({ALLOWED_START_HOUR}:00–{ALLOWED_END_HOUR}:00) gönderdi."
            )
        except Exception:
            pass

        # Thread'i kapat
        try:
            await thread.close(
                closer=self.bot.user,
                silent=True,
                delete_channel=False,
            )
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(RestrictedHours(bot))
