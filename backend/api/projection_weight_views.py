from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

# NOTE: ProjectionWeightAsset imported lazily inside methods to reduce startup time.
from .serializers import ProjectionWeightAssetSerializer


class ProjectionWeightAssetListView(generics.ListAPIView):
    from extraction.models import ProjectionWeightAsset
    queryset = ProjectionWeightAsset.objects.all().order_by("country_code", "region_name")
    serializer_class = ProjectionWeightAssetSerializer


class ProjectionWeightAssetDetailView(APIView):
    def get(self, request, country_code, *args, **kwargs):
        from extraction.models import ProjectionWeightAsset
        asset = (
            ProjectionWeightAsset.objects.filter(country_code__iexact=country_code)
            .order_by("-updated_at")
            .first()
        )
        if not asset:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = ProjectionWeightAssetSerializer(asset)
        return Response(serializer.data)

    def delete(self, request, country_code, *args, **kwargs):
        from extraction.models import ProjectionWeightAsset
        assets = ProjectionWeightAsset.objects.filter(country_code__iexact=country_code)
        deleted, _ = assets.delete()
        return Response({"deleted": deleted}, status=status.HTTP_200_OK)


class ProjectionWeightLearnView(APIView):
    def post(self, request, *args, **kwargs):
        from django.core.management import call_command

        country = request.data.get("country") or request.data.get("country_code")
        iso_code = request.data.get("iso_code")

        if not country and not iso_code:
            return Response(
                {"error": "'country' or 'country_code' is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        grid_size = int(request.data.get("grid_size", 4))
        min_weight = float(request.data.get("min_weight", 0.5))
        max_weight = float(request.data.get("max_weight", 2.0))
        max_samples = int(request.data.get("max_samples", 50000))

        cmd_args = ["--country", str(country)]
        if iso_code:
            cmd_args.extend(["--iso-code", str(iso_code)])
        cmd_args.extend(["--grid-size", str(grid_size)])
        cmd_args.extend(["--min-weight", str(min_weight)])
        cmd_args.extend(["--max-weight", str(max_weight)])
        cmd_args.extend(["--max-samples", str(max_samples)])

        call_command("learn_projection_weights", *cmd_args)

        # Return latest asset for this country if it exists
        from extraction.models import ProjectionWeightAsset
        asset = (
            ProjectionWeightAsset.objects.filter(country_code__iexact=iso_code or country)
            .order_by("-updated_at")
            .first()
        )
        if not asset:
            return Response({"status": "completed", "asset": None})

        serializer = ProjectionWeightAssetSerializer(asset)
        return Response({"status": "completed", "asset": serializer.data})
