from rest_framework import serializers

from mkt.webapps.utils import app_to_dict

from .models import Collection


class CollectionMembershipField(serializers.RelatedField):
    def to_native(self, value):
        return app_to_dict(value.app)


class CollectionSerializer(serializers.ModelSerializer):
    name = serializers.CharField()
    description = serializers.CharField()
    collection_type = serializers.IntegerField()
    apps = CollectionMembershipField(many=True,
                                     source='collectionmembership_set')

    class Meta:
        fields = ('apps', 'author', 'carrier', 'category', 'collection_type',
                  'description', 'id', 'is_public', 'name', 'region',)
        model = Collection
