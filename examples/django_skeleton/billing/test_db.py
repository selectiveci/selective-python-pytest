import pytest
from django.test import TestCase

from billing.models import Invoice
from conftest import bump


@pytest.mark.django_db
def test_rolls_back_first():
    Invoice.objects.create(total=1)
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
def test_rolls_back_second():
    Invoice.objects.create(total=2)
    assert Invoice.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_transactional():
    Invoice.objects.create(total=3)
    assert Invoice.objects.count() == 1


class InvoiceCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        bump("setUpTestData")
        Invoice.objects.create(total=10)

    def test_sees_class_data(self):
        Invoice.objects.create(total=11)
        assert Invoice.objects.count() == 2

    def test_class_data_is_rolled_back(self):
        assert Invoice.objects.count() == 1
