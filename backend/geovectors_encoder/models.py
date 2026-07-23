from django.contrib.gis.db import models

class GeoVectorsNle(models.Model):
    osm_key = models.CharField(max_length=255, primary_key=True)
    location = models.PointField(srid=4326)

    class Meta:
        db_table = 'geovectors_nle'
        verbose_name = 'GeoVectors NLE'
        verbose_name_plural = 'GeoVectors NLE'

    def __str__(self):
        return f"{self.osm_key} at {self.location}"
