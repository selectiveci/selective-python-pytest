from django.db import models


class Invoice(models.Model):
    total = models.IntegerField(default=0)
