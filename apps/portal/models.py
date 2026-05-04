from decimal import Decimal

from django.db import models

from apps.core.models import TimeStampedModel


class PortalCart(TimeStampedModel):
	STATUS_ACTIVE = "active"
	STATUS_QUOTED = "quoted"
	STATUS_INVOICED = "invoiced"
	STATUS_ABANDONED = "abandoned"

	STATUS_CHOICES = [
		(STATUS_ACTIVE, "Active"),
		(STATUS_QUOTED, "Quoted"),
		(STATUS_INVOICED, "Invoiced"),
		(STATUS_ABANDONED, "Abandoned"),
	]

	user = models.ForeignKey(
		"accounts.User",
		on_delete=models.CASCADE,
		related_name="portal_carts",
	)
	created_by_staff = models.ForeignKey(
		"accounts.User",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="portal_carts_created",
	)
	status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
	quote = models.ForeignKey(
		"billing.Quote",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="portal_carts",
	)
	invoice = models.ForeignKey(
		"billing.Invoice",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="portal_carts",
	)
	submitted_at = models.DateTimeField(null=True, blank=True)
	promo_code = models.ForeignKey(
		"core.PromoCode",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="carts",
	)

	class Meta:
		ordering = ["-created_at"]

	def __str__(self):
		return f"Cart #{self.pk} for {self.user.email}"

	@property
	def subtotal(self) -> Decimal:
		return sum((item.line_total for item in self.items.all()), Decimal("0.00"))

	@property
	def discount_amount(self) -> Decimal:
		if self.promo_code and self.promo_code.is_valid():
			sub = self.subtotal
			return (sub - self.promo_code.apply(sub)).quantize(Decimal("0.01"))
		return Decimal("0.00")

	@property
	def total(self) -> Decimal:
		return self.subtotal - self.discount_amount


class PortalCartItem(TimeStampedModel):
	TYPE_HOSTING = "hosting"
	TYPE_DOMAIN_REGISTRATION = "domain_registration"
	TYPE_DOMAIN_RENEWAL = "domain_renewal"
	TYPE_DOMAIN_TRANSFER = "domain_transfer"

	ITEM_TYPE_CHOICES = [
		(TYPE_HOSTING, "Hosting / service"),
		(TYPE_DOMAIN_REGISTRATION, "Domain registration"),
		(TYPE_DOMAIN_RENEWAL, "Domain renewal"),
		(TYPE_DOMAIN_TRANSFER, "Domain transfer"),
	]

	BILLING_MONTHLY = "monthly"
	BILLING_ANNUALLY = "annually"
	BILLING_PERIOD_CHOICES = [
		(BILLING_MONTHLY, "Monthly"),
		(BILLING_ANNUALLY, "Annually"),
	]

	cart = models.ForeignKey(PortalCart, on_delete=models.CASCADE, related_name="items")
	item_type = models.CharField(max_length=32, choices=ITEM_TYPE_CHOICES)
	description = models.CharField(max_length=255)
	quantity = models.PositiveSmallIntegerField(default=1)
	unit_price = models.DecimalField(max_digits=10, decimal_places=2)
	sort_order = models.PositiveSmallIntegerField(default=0)

	package = models.ForeignKey(
		"products.Package",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="portal_cart_items",
	)
	billing_period = models.CharField(
		max_length=20,
		choices=BILLING_PERIOD_CHOICES,
		blank=True,
	)

	domain_name = models.CharField(max_length=255, blank=True)
	registration_years = models.PositiveSmallIntegerField(default=1)
	transfer_auth_code = models.CharField(max_length=255, blank=True)
	domain = models.ForeignKey(
		"domains.Domain",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="portal_cart_items",
	)
	domain_contact = models.ForeignKey(
		"domains.DomainContact",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="portal_cart_items",
	)
	privacy_enabled = models.BooleanField(default=True)
	auto_renew = models.BooleanField(default=True)
	dns_provider = models.CharField(
		max_length=20,
		choices=[
			("registrar", "Registrar"),
			("cpanel", "cPanel"),
			("cloudflare", "Cloudflare"),
			("external", "External"),
		],
		default="cpanel",
	)

	class Meta:
		ordering = ["sort_order", "id"]

	def __str__(self):
		return self.description

	@property
	def line_total(self) -> Decimal:
		return (Decimal(str(self.quantity)) * self.unit_price).quantize(Decimal("0.01"))
