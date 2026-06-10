from django.urls import path
from . import views

urlpatterns = [
    path('',               views.index,       name='index'),
    path('upload/',        views.upload,      name='upload'),
    path('progress/<str:job_id>/', views.progress, name='progress'),
    path('results/<str:job_id>/',  views.results,  name='results'),
    path('delete-cluster/<str:job_id>/<int:cluster_id>/<int:page_index>/',
         views.delete_page, name='delete_page'),
]
