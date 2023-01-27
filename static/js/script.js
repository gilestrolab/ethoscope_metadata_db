// materialize css tab menu
// A Pen created on CodePen.io. Original URL: [https://codepen.io/frippa/pen/amBzrv](https://codepen.io/frippa/pen/amBzrv).
// material fixed tab menu with materialize css

$(".button-collapse").sideNav();

var current = {
    authors_list : [],
    tags_list : []
}

var info_tree = {};

var formData = new FormData();
var xhr = new XMLHttpRequest();


"use strict";

// Only start once the DOM tree is ready
if(document.readyState === "complete") {
    createDropzoneMethods();
} else {
    document.addEventListener("DOMContentLoaded", createDropzoneMethods);
}


function populate_select_with_projects() {
    $.get( '/info_tree' ).success( function( data ) {
        $.each(data, function (key, entry) {
            $('#project-select').append($('<option></option>').attr('value', key).text(key));
          })
        $("#project-select").material_select();
    })
}

function update_browser(identifier) {
    $.get( '/metadata', {'id' : identifier} ).success( function( data ){
        console.log(data);
        $('#browser-table').show();

        $('#browser_project').html (data['project']);
        $('#browser_authors').html ( data['authors'].toString() );
        $('#browser_tags').html ( data['tags'].toString() );
        $('#browser_description').html (data['description'].toString());
        
        if (data['paper_doi']) {
            $('#browser_doi').attr("href", data['paper_doi'] );
            $('#browser_doi').show();
        } else {
            $('#browser_doi').hide();
        }

        $('#browser_download_link').attr("href", "/download?type=metadata&id="+data['identifier'] );
        $('#browser_refresh').attr("onclick", "refresh_metadata_db('"+data['identifier']+"');" );

        $('#browser_db_total').html (
            data['db_files'] + ' databases <a href="/download?type=db_found&id=' +data['identifier']+ '" class="fa fa-download" style="color:grey" ></a>');

        $('#browser_db_not_found').html (
            data['db_files_na'] + ' databases <a href="/download?type=db_notfound&id=' +data['identifier']+ '" class="fa fa-download" style="color:grey"></a>');

        $('#browser_db_size').html (data['db_files_size']);
        $('#browser_entries_total').html (data['entries'] + ' entries');
        $('#browser_entries_not_found').html (data['entries_not_found'] + ' entries');

    })
}


function refresh_metadata_db(identifier) {
    $('#refresh_modal').show();
    $('#refresh_modal').focus();
    $('#modal_text').html('Associating metadata to DB files. This may take a few seconds.');
    $('#modal_btn').addClass('disabled');   
    $('#modal_progress_bar').show();
    
    $.ajax({
            url: '/refresh',
            timeout: 60 * 1000,
            data : {'id' : identifier},
            type: 'GET'
        }).success( function( data ){
            $('#modal_progress_bar').hide();
            $('#modal_text').html(data.message);
            $('#modal_btn').removeClass('disabled');

            $('#modal_btn').click(function(){ 
                $('#refresh_modal').hide();
            });


    })
}

function refresh_info () {
    $.get( '/info_tree' ).success( function( data ) {
        $('#project-list').html('');
        let prj_name = $("#project-select").val();
        $.each(data[prj_name], function (filename, identifier) {
            $('#project-list').append($('\
                                <li><a href="#" class="collection-item metadata_name" onclick="update_browser(\''+identifier+'\');return false;" style="display:inline-block">'+filename+'</a>\
                                <a class="fa fa-trash" title="Delete metadata" style="color:sky;float:right;margin-top:15px;" onclick="delete_metadata(\''+identifier+'\');"></a>\
                                <a class="fa fa-edit disabled"  title="Edit metadata - not yet implemented" style="color:lightgrey;float:right;margin-top:15px;margin-right:20px;" onclick="edit_metadata(\''+identifier+'\');"></a>\
                                </li>'));
          })
    })
}

function edit_metadata(identifier) {
    //location.reload();
}

function delete_metadata(identifier) {

    $('#delete_modal').show();
    $('#delete_modal').focus();

    $('#modal_delete_cancel_btn').click(function(){ 
        $('#delete_modal').hide();
    });

    $('#modal_delete_confirm_btn').click(function(){ 
        $.ajax({
            url: '/delete',
            timeout: 60 * 1000,
            data : {'id' : identifier},
            type: 'GET'
        }).success( function( data ){
            $('#modal_delete_text').html(data.message);
            $('#modal_delete_confirm_btn').addClass("disabled");
            $('#modal_delete_cancel_btn').text("OK");
        });
});
}

// INITIALIZATION OF AUTOCOMPLETE LIST
function get_available_options () {
    $.get( '/known').success( function( data ){
        let available = {};

        available.authors = Object.fromEntries(data['authors'].map(x => [x, null]));
        available.project = Object.fromEntries(data['project'].map(x => [x, null]));
        available.tags = Object.fromEntries(data['tags'].map(x => [x, null]));
        available.paper_doi = Object.fromEntries(data['paper_doi'].map(x => [x, null]));
        
        $("#project-input").autocomplete({
            data: available.project
        });

        $("#doi-input").autocomplete({
            data: available.tags
        });

        $("#authors-input").autocomplete({
            data: available.authors
        });

        $("#tags-input").autocomplete({
            data: available.tags
        });
    });
};

// Display Chips
function displayChips(divID) {

    $('#' + divID).material_chip({
        data: current[divID]
    });
}    

function redirectFocus( from, dest) {    
    $(from).focusin(function () {
        $(dest).focus();
    });
}

// ADDING A NEW CHIP
function addChip ( divID ){
    
    input = divID + "-input";
    list = divID + "_list";
    
    chipName = $("#" + input).val().toLowerCase();
    
    // test1 : minimum word size
    if (!(chipName.length > 2)){
        return 0;
    }
    // test2 :  no duplicates
    for(i=0;i<current[list].length;i++) {
        if(chipName == current[list][i].tag){
            return 0;
        }
    }
    // tests Okay => add the chip and refresh the view
    current[list].push( {"tag" : chipName });
   
    displayChips(list);
    $("#" + input).val("");
    
    return 1;
};

function createDropzoneMethods() {
    let dropzone = document.getElementById("dropzone_element");

    dropzone.ondragover = function() {
        this.className = "dropzone dragover";
        return false;
    }
    
    dropzone.ondragleave = function() {
        this.className = "dropzone";
        return false;
    }

    dropzone.ondrop = function(e) {
        // Stop browser from simply opening that was just dropped
        e.preventDefault();  
        
        // Restore original dropzone appearance
        //this.className = "dropzone";

        dropzone.innerHTML = e.dataTransfer.files[0].name;

        add_file_to_form(e.dataTransfer.files)
    }    
}

function add_file_to_form(files) {
    for(let i=0; i<files.length; i++) {
        formData.append('file', files[i]);
    }
}


function submit_form() {

    formData.append ('project', $("#project-input").val());
    formData.append ('doi', $("#doi-input").val());
    formData.append ('description', $("#description-input").val());
    formData.append ('authors', JSON.stringify(Object.entries(current.authors_list).map(([key, value]) => ( value['tag']))) );
    formData.append ('tags', JSON.stringify(Object.entries(current.tags_list).map(([key, value]) => ( value['tag']))) );


    xhr.onreadystatechange = function() {
        if(xhr.readyState === XMLHttpRequest.DONE) {

            let result = JSON.parse(xhr.response);

            $("#upload_result_element").attr("hidden", false);

            if (result.success == false) {
                //make alert
                $("#upload_result_element").attr('class', 'alert card red lighten-4 red-text text-darken-4" id="upload_result_element');
                //$("#upload-banner-icon").innerHTML = "report";
                $("#upload_result_message").html("<i class=\"material-icons\" id=\"upload-banner-icon\">report</i><span>Error:</span>" + result.message);
            }

            if (result.success == true) {
                //make green
                $("#upload_result_element").attr('class', 'alert card green lighten-4 green-text text-darken-4" id="upload_result_element');
                //$("#upload-banner-icon").innerHTML = "check_circle";
                $("#upload_result_message").html("<i class=\"material-icons\" id=\"upload-banner-icon\">check_circle</i><span>OK:</span>" + result.message);
            }
        }
    }

    xhr.open('POST', '/save', true); // async = true
    xhr.send(formData); 
}

$(function() {
// delete chip command
    $('#current_authors_list').on('chip.delete', function(e, chip){
        current_authors = $("#current_authors_list").material_chip('data');
    });

    $('.modal').modal();

    get_available_options();

    redirectFocus('#authors_list', '#authors-input');
    redirectFocus('#tags_list', '#tags-input');

    displayChips('authors_list');
    displayChips('tags_list');

    $("#add_authors_BTN").click(function () {
        addChip( 'authors' );
    });

    $("#add_tags_BTN").click(function () {
        addChip( 'tags' );
    });

    $("#save_BTN").click(function () {
        submit_form();
    });

    $('#project-select').material_select();
    populate_select_with_projects();

    $('#project-select').on('change', function() {
        refresh_info();
    });

});