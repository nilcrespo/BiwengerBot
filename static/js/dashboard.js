document.addEventListener('DOMContentLoaded', function() {
    // Fetch data for charts
    fetch('/performance-data')
        .then(response => response.json())
        .then(data => {
            const layout = {
                title: 'Player Performance Trends',
                xaxis: { title: 'Date' },
                yaxis: { title: 'Points' },
                hovermode: 'closest'
            };
            
            Plotly.newPlot('performance-chart', data, layout);
        });
});